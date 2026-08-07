from __future__ import annotations

import asyncio
import logging
import ssl
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup, NavigableString

from school_discord_bot.models.curriculum import ClassTimetable, Lesson


BASE_URL = "https://campus.dali.tc.edu.tw/Curriculum/Webpaik.aspx"

# Grade names as the server delivers them.
ALL_GRADES: tuple[str, ...] = ("1年級", "2年級", "3年級")

# ASP.NET WebForms hidden-field names that carry all server-side state.
_STATE_FIELDS = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__EVENTVALIDATION",
    "__PREVIOUSPAGE",
)


def parse_timetable_page(html: str, *, class_code: str, grade: str) -> ClassTimetable | None:
    """Parse a timetable result page into a ClassTimetable.

    Returns ``None`` when the page carries no timetable for this class. The
    site reports that in two ways: an explicit ``查無資料`` message in
    ``Lab_msg``, or a page with no ``Lab_name_id`` echo at all.

    A class that is listed in the dropdown but has no lessons scheduled (not
    every class takes part in the summer supplementary term) is a legitimate
    "no data" answer, not a scraping failure. Conversely a class may return
    ``ok`` with only a handful of populated cells — a sparse grid is still a
    valid timetable and is preserved as-is.
    """
    soup = BeautifulSoup(html, "lxml")

    msg_el = soup.find(id="ContentPlaceHolder1_Lab_msg")
    message = msg_el.get_text(strip=True) if msg_el else ""
    if not message or "查無資料" in message:
        return None

    id_el = soup.find(id="ContentPlaceHolder1_Lab_name_id")
    if not id_el or not id_el.get_text(strip=True):
        return None

    schedule_title = ""
    sys_el = soup.find(id="ContentPlaceHolder1_Lab_sys")
    if sys_el:
        schedule_title = sys_el.get_text(strip=True)

    homeroom = ""
    f_el = soup.find(id="ContentPlaceHolder1_Lab_name_f")
    if f_el:
        homeroom = f_el.get_text(strip=True)

    lessons: list[Lesson] = []
    for day in range(5):
        for period in range(1, 10):
            idx = day * 10 + period
            a = soup.find(id=f"ContentPlaceHolder1_name{idx}")
            if a is None:
                continue
            td = a.find_parent("td")
            if td is None:
                continue
            # Subject is a bare NavigableString directly inside the td, not
            # wrapped in any element.  Concatenate all such text nodes.
            subject = "".join(
                c.strip() for c in td.children if isinstance(c, NavigableString)
            ).strip()
            if not subject:
                continue
            teachers: list[str] = []
            t = a.get_text(strip=True)
            if t:
                teachers.append(t)
            b_el = soup.find(id=f"ContentPlaceHolder1_nameB{idx}")
            if b_el:
                bt = b_el.get_text(strip=True)
                if bt:
                    teachers.append(bt)
            lessons.append(Lesson(day=day, period=period, subject=subject, teachers=teachers))

    return ClassTimetable(
        class_code=class_code,
        grade=grade,
        schedule_title=schedule_title,
        homeroom_teacher=homeroom,
        lessons=lessons,
    )


class CurriculumClient:
    """Scrape class timetables from the ASP.NET WebForms timetable site.

    The site uses ViewState-based POST chains; every response must have its
    hidden state fields extracted before the next request can be issued.

    SSL note: ``campus.dali.tc.edu.tw`` has a certificate that fails Python's
    strict verification (missing Subject Key Identifier extension).  When
    ``allow_insecure_ssl_fallback`` is True the client will retry with cert
    verification disabled, but *only* for requests to this exact host — the
    same host-pinning guard used by ``SchoolNewsClient``.
    """

    REQUEST_DELAY_SECONDS = 0.3  # polite inter-request pause for the ~54 prefetch POSTs

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        base_url: str = BASE_URL,
        timeout_seconds: int = 20,
        user_agent: str = "SchoolDiscordBot/0.1",
        allow_insecure_ssl_fallback: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self.session = session
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.allow_insecure_ssl_fallback = allow_insecure_ssl_fallback
        self.logger = logger or logging.getLogger(__name__)
        self._trusted_host = (urlparse(base_url).hostname or "").lower()
        self._insecure_ssl_context = self._build_insecure_ssl_context()
        # Latched once the insecure fallback is known to be needed, so we stop
        # paying for a doomed TLS handshake on every subsequent request.
        self._insecure_ssl_confirmed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_class_timetable(self, class_code: str) -> ClassTimetable | None:
        """Fetch a single class timetable.  Performs all three setup POSTs.

        Returns ``None`` for an unknown class code.  The code is validated
        against the grade's rendered option list before posting, because
        ASP.NET event validation rejects an unlisted value with an HTTP 500
        rather than a friendly error page.
        """
        grade = _grade_for_code(class_code)
        if grade is None:
            return None
        soup = await self._get_initial_page()
        soup = await self._switch_to_class_mode(soup)
        soup = await self._select_grade(soup, grade)

        if class_code not in _class_codes_in(soup):
            self.logger.info("Class %s is not offered in %s", class_code, grade)
            return None

        html = await self._fetch_class_html(soup, grade, class_code)
        if not html:
            return None
        return parse_timetable_page(html, class_code=class_code, grade=grade)

    async def fetch_grade_classes(self, grade: str) -> list[str]:
        """Return the class codes the site currently offers for ``grade``."""
        soup = await self._get_initial_page()
        soup = await self._switch_to_class_mode(soup)
        soup = await self._select_grade(soup, grade)
        return _class_codes_in(soup)

    async def fetch_all(self) -> list[ClassTimetable]:
        """Prefetch timetables for all known classes.

        Uses the state-chaining optimisation: after paying the three-POST
        setup cost once per grade, subsequent classes in that grade each
        require only one additional POST.  Failures for individual classes are
        logged and skipped rather than aborting the whole sync.
        """
        results: list[ClassTimetable] = []
        skipped: list[str] = []
        failed: list[str] = []
        expected = 0

        soup = await self._get_initial_page()
        soup = await self._switch_to_class_mode(soup)

        for grade in ALL_GRADES:
            grade_soup = await self._select_grade(soup, grade)
            class_codes = _class_codes_in(grade_soup)
            expected += len(class_codes)
            self.logger.info("Prefetching %s classes for %s", len(class_codes), grade)

            current_state_soup = grade_soup
            for code in class_codes:
                await asyncio.sleep(self.REQUEST_DELAY_SECONDS)
                try:
                    html = await self._fetch_class_html(current_state_soup, grade, code)
                    tt = parse_timetable_page(html, class_code=code, grade=grade)

                    if tt is not None:
                        results.append(tt)
                        # The result page itself carries fresh state for the
                        # next class, which is what makes this loop cheap.
                        current_state_soup = BeautifulSoup(html, "lxml")
                        continue

                    # No timetable. Either this class genuinely has none, or
                    # our chained state went stale. Re-run setup once and retry
                    # to tell the two apart.
                    fresh = await self._get_initial_page()
                    fresh = await self._switch_to_class_mode(fresh)
                    current_state_soup = await self._select_grade(fresh, grade)
                    html = await self._fetch_class_html(current_state_soup, grade, code)
                    tt = parse_timetable_page(html, class_code=code, grade=grade)

                    if tt is not None:
                        results.append(tt)
                        current_state_soup = BeautifulSoup(html, "lxml")
                    else:
                        # Confirmed with fresh state: this class has no
                        # timetable published (common for classes not taking
                        # part in the current term).
                        skipped.append(code)

                except Exception:
                    self.logger.exception("Failed to fetch timetable for class %s", code)
                    failed.append(code)

        if skipped:
            self.logger.info(
                "%s class(es) had no published timetable: %s",
                len(skipped),
                ", ".join(skipped),
            )
        if failed:
            self.logger.error(
                "%s class(es) failed with an exception: %s",
                len(failed),
                ", ".join(failed),
            )
        self.logger.info(
            "Prefetch complete: %s fetched, %s no-data, %s failed (of %s)",
            len(results),
            len(skipped),
            len(failed),
            expected,
        )
        return results

    # ------------------------------------------------------------------
    # Internal: ASP.NET form flow
    # ------------------------------------------------------------------

    async def _get_initial_page(self) -> BeautifulSoup:
        response = await self._request("GET", self.base_url)
        html = await response.text()
        return BeautifulSoup(html, "lxml")

    async def _switch_to_class_mode(self, soup: BeautifulSoup) -> BeautifulSoup:
        """Step 1: POST to switch from teacher-mode to class-mode."""
        data = _extract_state(soup)
        data["ctl00$ContentPlaceHolder1$But_clas"] = "班名"
        data["ctl00$ContentPlaceHolder1$Drop_group"] = ""
        response = await self._request("POST", self.base_url, data=data)
        html = await response.text()
        return BeautifulSoup(html, "lxml")

    async def _select_grade(self, soup: BeautifulSoup, grade: str) -> BeautifulSoup:
        """Step 2: POST to trigger AutoPostBack on grade dropdown."""
        data = _extract_state(soup)
        data["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$Drop_group"
        data["__EVENTARGUMENT"] = ""
        data["ctl00$ContentPlaceHolder1$Drop_group"] = grade
        response = await self._request("POST", self.base_url, data=data)
        html = await response.text()
        return BeautifulSoup(html, "lxml")

    async def _fetch_class_html(
        self, soup: BeautifulSoup, grade: str, class_code: str
    ) -> str:
        """Step 3: POST to confirm class selection and get the timetable grid.

        Returns an empty string when the server rejects the selection (it
        answers HTTP 500 when ``__EVENTVALIDATION`` does not recognise the
        posted class code), so callers can treat it as "no such class".
        """
        data = _extract_state(soup)
        data["ctl00$ContentPlaceHolder1$Drop_group"] = grade
        data["ctl00$ContentPlaceHolder1$List_name"] = class_code
        data["ctl00$ContentPlaceHolder1$Butn_prin"] = "確認"
        try:
            response = await self._request("POST", self.base_url, data=data)
        except aiohttp.ClientResponseError as exc:
            self.logger.warning(
                "Server rejected class selection %s (%s): HTTP %s",
                class_code,
                grade,
                exc.status,
            )
            return ""
        return await response.text()

    # ------------------------------------------------------------------
    # Internal: HTTP with SSL fallback (mirrors SchoolNewsClient)
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> aiohttp.ClientResponse:
        context = self._insecure_ssl_context if self._insecure_ssl_confirmed else None
        return await self._request_with_ssl(method, url, data=data, ssl_context=context)

    async def _request_with_ssl(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None,
        ssl_context: ssl.SSLContext | None,
    ) -> aiohttp.ClientResponse:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"User-Agent": self.user_agent}
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = await self.session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                    ssl=ssl_context,
                )
                response.raise_for_status()
                return response
            except aiohttp.ClientResponseError:
                # An HTTP status error is a deliberate server answer; retrying
                # the identical request will not change it.
                raise
            except aiohttp.ClientError as exc:
                if ssl_context is None and self._should_use_insecure_ssl_fallback(url, exc):
                    if not self._insecure_ssl_confirmed:
                        self.logger.warning(
                            "TLS verification failed for %s. Falling back to unverified TLS "
                            "for this trusted school host for the rest of this session.",
                            url,
                        )
                        self._insecure_ssl_confirmed = True
                    return await self._request_with_ssl(
                        method, url, data=data, ssl_context=self._insecure_ssl_context
                    )
                last_error = exc
                if attempt == 2:
                    break
                delay = 2**attempt
                self.logger.warning("Request %s %s attempt %s/3 failed: %s", method, url, attempt + 1, exc)
                await asyncio.sleep(delay)
            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt == 2:
                    break
                self.logger.warning("Request %s %s attempt %s/3 timed out", method, url, attempt + 1)
                await asyncio.sleep(2**attempt)

        raise aiohttp.ClientError(f"All retries exhausted for {method} {url}") from last_error

    def _should_use_insecure_ssl_fallback(self, url: str, exc: aiohttp.ClientError) -> bool:
        if not self.allow_insecure_ssl_fallback:
            return False
        request_host = (urlparse(url).hostname or "").lower()
        if not request_host or request_host != self._trusted_host:
            return False
        if isinstance(exc, aiohttp.ClientConnectorCertificateError):
            return True
        cause = getattr(exc, "__cause__", None)
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, aiohttp.ClientSSLError):
            ssl_errors = getattr(exc, "args", ())
            return any(
                isinstance(item, ssl.SSLCertVerificationError)
                or "certificate verify failed" in str(item).lower()
                for item in ssl_errors
            )
        return False

    @staticmethod
    def _build_insecure_ssl_context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _extract_state(soup: BeautifulSoup) -> dict[str, str]:
    """Pull the four hidden ViewState fields from a soup."""
    state: dict[str, str] = {}
    for name in _STATE_FIELDS:
        el = soup.find("input", {"name": name})
        if el:
            state[name] = el.get("value", "")
    return state


def _class_codes_in(soup: BeautifulSoup) -> list[str]:
    """Read the class codes currently offered by the List_name select."""
    return [
        text
        for o in soup.select('select[name$="List_name"] option')
        if (text := o.get_text(strip=True))
    ]


def _grade_for_code(class_code: str) -> str | None:
    """Derive the grade string from a class code, e.g. '205' → '2年級'."""
    if len(class_code) == 3 and class_code[0].isdigit():
        digit = class_code[0]
        if digit in ("1", "2", "3"):
            return f"{digit}年級"
    return None
