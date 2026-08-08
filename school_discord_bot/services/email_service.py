from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

logger = logging.getLogger(__name__)


class EmailService:
    """Async SMTP email sender for the student verification flow."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        encryption: str,        # "ssl" | "starttls" | "none"
        username: str,
        password: str,
        from_address: str,
        from_name: str,
    ) -> None:
        self._host = host
        self._port = port
        self._encryption = encryption
        self._username = username
        self._password = password
        self._from_address = from_address
        self._from_name = from_name

    async def send_verification_code(
        self,
        to_address: str,
        student_id: str,
        code: str,
    ) -> None:
        """Send a verification email containing the 6-digit code.

        Args:
            to_address: The recipient's email address (derived from student ID).
            student_id: The student's school ID, included in the email body.
            code: The 6-digit one-time verification code.
        """
        msg = EmailMessage()
        msg["Subject"] = "大里高中 Discord 學生驗證碼"
        msg["From"] = f"{self._from_name} <{self._from_address}>"
        msg["To"] = to_address
        msg.set_content(
            f"你好，\n\n"
            f"你的 Discord 學生驗證碼為：\n\n"
            f"    {code}\n\n"
            f"請在 15 分鐘內於 Discord 輸入此驗證碼完成驗證。\n"
            f"學號：{student_id}\n\n"
            f"如果你沒有提出此請求，請忽略此封信件。\n\n"
            f"大里高中 Discord 機器人"
        )

        # aiosmtplib:
        #   use_tls=True  → wraps connection in TLS from the start (port 465 / SMTP_SSL)
        #   start_tls=True → plain connect then upgrades via STARTTLS (port 587)
        #   both False     → plain SMTP (useful for local dev relays)
        use_tls = self._encryption == "ssl"
        start_tls = self._encryption == "starttls"

        async with aiosmtplib.SMTP(
            hostname=self._host,
            port=self._port,
            use_tls=use_tls,
            start_tls=start_tls,
        ) as smtp:
            await smtp.login(self._username, self._password)
            await smtp.send_message(msg)

        logger.info(
            "Sent verification code to %s (student_id=%s)",
            to_address,
            student_id,
        )
