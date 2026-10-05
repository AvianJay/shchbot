from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.app_commands.namespace import ResolveKey
from PIL import ExifTags, Image
import pytest

from school_discord_bot.cogs.anonymous_board import (
    AnonymousBoardCog,
    ReasonModal,
    ReviewActionButton,
    SubmissionModal,
    build_public_embeds,
    build_review_embed,
    review_view,
    sanitize_image,
)
from school_discord_bot.db.database import Database
from school_discord_bot.models.anonymous_board import (
    DEFAULT_CATEGORIES,
    MAX_CATEGORIES,
    SUBMIT_COOLDOWN_SECONDS,
    AnonymousBoardConfig,
    AnonymousCategory,
    AnonymousPost,
    PostStatus,
    setting_key,
    thread_name,
)


GUILD_ID = 456
PUBLIC_ID = 100
REVIEW_ID = 200
AUTHOR_ID = 111111111111111111
MODERATOR_ID = 222222222222222222
CATEGORY = AnonymousCategory(1, "😡 我要靠北")
SECRET_FILENAME = "IMG_王小明_secret.jpg"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def http_error(cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return cls(SimpleNamespace(status=status, reason="error"), "")


class FakeMessage:
    def __init__(self, message_id: int, attachments: list[object] | None = None) -> None:
        self.id = message_id
        self.attachments = attachments or []
        self.create_thread = AsyncMock()


class FakeAttachment:
    def __init__(self, data: bytes, *, filename: str = SECRET_FILENAME, size: int | None = None) -> None:
        self.filename = filename
        self.size = len(data) if size is None else size
        self.read = AsyncMock(return_value=data)


def make_text_channel(
    channel_id: int,
    *,
    everyone_can_view: bool = False,
    bot_permissions: discord.Permissions | None = None,
) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.mention = f"<#{channel_id}>"
    channel.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel_id}"
    default_role, me = object(), object()
    channel.guild = SimpleNamespace(id=GUILD_ID, me=me, default_role=default_role)
    granted = bot_permissions or discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        create_public_threads=True,
        manage_threads=True,
    )

    def permissions_for(target: object) -> discord.Permissions:
        if target is default_role:
            return discord.Permissions(view_channel=everyone_can_view)
        return granted

    channel.permissions_for.side_effect = permissions_for

    async def send(*args: object, **kwargs: object) -> FakeMessage:
        await asyncio.sleep(0)  # yield, so concurrent callers actually interleave
        return FakeMessage(channel_id * 1000 + channel.send.await_count)

    channel.send = AsyncMock(side_effect=send)
    return channel


class FakeBot:
    def __init__(self, channels: list[MagicMock]) -> None:
        self.channels = {channel.id: channel for channel in channels}
        self.threads: dict[int, object] = {}
        self.partials: dict[tuple[int, int], SimpleNamespace] = {}
        self.dm = SimpleNamespace(send=AsyncMock())
        self.create_dm = AsyncMock(return_value=self.dm)
        self.fetch_channel = AsyncMock(side_effect=http_error(discord.NotFound, 404))
        self.guild = SimpleNamespace(get_thread=lambda thread_id: self.threads.get(thread_id))

    def get_channel(self, channel_id: int) -> object:
        return self.channels.get(channel_id)

    def get_guild(self, guild_id: int) -> SimpleNamespace:
        return self.guild

    def get_partial_messageable(self, channel_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            get_partial_message=lambda message_id: self.partial(channel_id, message_id)
        )

    def partial(self, channel_id: int, message_id: int) -> SimpleNamespace:
        key = (channel_id, message_id)
        if key not in self.partials:
            self.partials[key] = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        return self.partials[key]


def make_interaction(
    *,
    user_id: int = AUTHOR_ID,
    manage_messages: bool = False,
    message: FakeMessage | None = None,
) -> SimpleNamespace:
    state = {"done": False}

    async def respond(*args: object, **kwargs: object) -> None:
        state["done"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, name="student", display_name="小明"),
        response=SimpleNamespace(
            defer=AsyncMock(side_effect=respond),
            send_message=AsyncMock(side_effect=respond),
            send_modal=AsyncMock(side_effect=respond),
            is_done=lambda: state["done"],
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        permissions=discord.Permissions(manage_messages=manage_messages),
        message=message,
        filesize_limit=25 * 1024 * 1024,
    )


def last_reply(interaction: SimpleNamespace) -> str:
    if interaction.followup.send.await_args is not None:
        return interaction.followup.send.await_args.args[0]
    return interaction.response.send_message.await_args.args[0]


def assert_private(interaction: SimpleNamespace) -> None:
    """Anything non-ephemeral would show "X used …" next to the reply."""
    for mock in (
        interaction.response.defer,
        interaction.response.send_message,
        interaction.followup.send,
    ):
        for call in mock.await_args_list:
            assert call.kwargs.get("ephemeral") is True


def jpeg_with_metadata() -> bytes:
    image = Image.new("RGB", (40, 20), "red")
    exif = image.getexif()
    exif[ExifTags.Base.Orientation] = 6  # rotate 90° clockwise when displayed
    exif[ExifTags.Base.Make] = "SecretCam"
    exif[ExifTags.IFD.GPSInfo] = {
        ExifTags.GPS.GPSLatitudeRef: "N",
        ExifTags.GPS.GPSLatitude: (24.0, 8.0, 0.0),
    }
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes(), comment=b"owner: alice")
    return buffer.getvalue()


async def make_board(
    tmp_path: Path,
    *,
    require_review: bool = True,
    configured: bool = True,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> SimpleNamespace:
    database = Database(tmp_path / "board.sqlite3")
    await database.initialize()
    public = make_text_channel(PUBLIC_ID)
    review = make_text_channel(REVIEW_ID)
    bot = FakeBot([public, review])
    if configured:
        await database.set_setting(
            setting_key(GUILD_ID),
            AnonymousBoardConfig(PUBLIC_ID, REVIEW_ID, require_review).to_json(),
        )
    await database.seed_anonymous_categories(categories)
    cog = AnonymousBoardCog(bot, database=database, guild_id=GUILD_ID)
    return SimpleNamespace(database=database, public=public, review=review, bot=bot, cog=cog)


async def submit(board: SimpleNamespace, *, attachments: list[object] | None = None) -> SimpleNamespace:
    interaction = make_interaction()
    await board.cog.handle_submission(
        interaction,
        category=CATEGORY,
        content="今天午餐 @everyone <@333333333333333333>",
        attachments=attachments or [],
    )
    return interaction


def make_post(**overrides: object) -> AnonymousPost:
    values: dict[str, object] = {
        "id": 7,
        "author_id": AUTHOR_ID,
        "author_name": "小明 (@student)",
        "category_name": CATEGORY.name,
        "content": "內容",
        "image_count": 0,
        "status": PostStatus.PENDING,
        "created_at": 1_700_000_000.0,
    }
    values.update(overrides)
    return AnonymousPost(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def test_categories_add_remove_and_seed(tmp_path: Path) -> None:
    async def run() -> None:
        database = Database(tmp_path / "bot.sqlite3")
        await database.initialize()
        try:
            await database.seed_anonymous_categories(DEFAULT_CATEGORIES)
            await database.seed_anonymous_categories(DEFAULT_CATEGORIES)
            assert [c.name for c in await database.list_anonymous_categories()] == list(DEFAULT_CATEGORIES)

            assert await database.add_anonymous_category("🎉 我要分享") is True
            assert await database.add_anonymous_category("🎉 我要分享") is False
            assert await database.count_anonymous_categories() == len(DEFAULT_CATEGORIES) + 1

            assert await database.remove_anonymous_category(DEFAULT_CATEGORIES[0]) is True
            assert await database.remove_anonymous_category(DEFAULT_CATEGORIES[0]) is False
            names = [c.name for c in await database.list_anonymous_categories()]
            assert names == [*DEFAULT_CATEGORIES[1:], "🎉 我要分享"]
        finally:
            await database.close()

    asyncio.run(run())


def test_cooldown_is_enforced_by_the_insert_itself(tmp_path: Path) -> None:
    async def run() -> None:
        database = Database(tmp_path / "bot.sqlite3")
        await database.initialize()
        try:
            def create(author_id: int, created_at: float):
                return database.create_anonymous_post(
                    author_id=author_id,
                    author_name="x",
                    category_name="c",
                    content="內容",
                    image_count=0,
                    created_at=created_at,
                    cooldown_seconds=SUBMIT_COOLDOWN_SECONDS,
                )

            first = await create(AUTHOR_ID, 1000.0)
            assert first is not None
            # Second submission inside the window is refused even though the
            # cursor's lastrowid still points at the first row.
            assert await create(AUTHOR_ID, 1000.0 + SUBMIT_COOLDOWN_SECONDS - 1) is None
            assert await create(MODERATOR_ID, 1001.0) is not None
            later = await create(AUTHOR_ID, 1000.0 + SUBMIT_COOLDOWN_SECONDS + 1)
            assert later is not None and later != first
            assert await database.get_last_anonymous_post_at(AUTHOR_ID) == 1000.0 + SUBMIT_COOLDOWN_SECONDS + 1
        finally:
            await database.close()

    asyncio.run(run())


def test_status_changes_only_from_the_expected_state(tmp_path: Path) -> None:
    async def run() -> None:
        database = Database(tmp_path / "bot.sqlite3")
        await database.initialize()
        try:
            ids = []
            for index in range(2):
                post_id = await database.create_anonymous_post(
                    author_id=index,
                    author_name="x",
                    category_name="c",
                    content="內容",
                    image_count=0,
                    created_at=1.0,
                    cooldown_seconds=0,
                )
                ids.append(post_id)
            rejected_id, published_id = ids

            assert await database.mark_anonymous_post_rejected(
                rejected_id, moderator_id=MODERATOR_ID, reason="理由", moderated_at=2.0
            )
            assert not await database.mark_anonymous_post_rejected(
                rejected_id, moderator_id=MODERATOR_ID, reason=None, moderated_at=3.0
            )
            # A rejected post never consumes a public number.
            assert await database.next_anonymous_public_number() == 1
            assert not await database.mark_anonymous_post_published(
                rejected_id, public_number=1, channel_id=1, message_id=1, moderator_id=None, moderated_at=3.0
            )
            assert not await database.mark_anonymous_post_removed(
                published_id, moderator_id=MODERATOR_ID, reason=None, moderated_at=3.0
            )

            assert await database.mark_anonymous_post_published(
                published_id, public_number=1, channel_id=10, message_id=20, moderator_id=None, moderated_at=3.0
            )
            assert await database.next_anonymous_public_number() == 2
            assert await database.mark_anonymous_post_removed(
                published_id, moderator_id=MODERATOR_ID, reason="下架", moderated_at=4.0
            )
            removed = await database.get_anonymous_post(published_id)
            assert removed.status is PostStatus.REMOVED
            assert removed.public_number == 1  # numbers are never reused
            assert removed.moderator_id == MODERATOR_ID
            assert await database.count_anonymous_posts(PostStatus.REJECTED) == 1
        finally:
            await database.close()

    asyncio.run(run())


def test_config_round_trip() -> None:
    config = AnonymousBoardConfig(PUBLIC_ID, REVIEW_ID, False)
    assert AnonymousBoardConfig.from_json(config.to_json()) == config
    assert setting_key(GUILD_ID) == "anon_board:456"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_public_embeds_hide_the_author_and_share_one_gallery_url() -> None:
    post = make_post(content="@everyone 看 <@333333333333333333>")
    published_at = discord.utils.utcnow()
    filenames = ["anon_12_1.jpg", "anon_12_2.png", "anon_12_3.webp"]
    embeds = build_public_embeds(
        post, number=12, gallery_url="https://example.com/c", filenames=filenames, published_at=published_at
    )

    assert embeds[0].title == "#12 😡 我要靠北"
    assert embeds[0].timestamp == published_at
    assert "@​everyone" in embeds[0].description
    assert "<@​333333333333333333>" in embeds[0].description
    assert {embed.url for embed in embeds} == {"https://example.com/c"}
    assert [embed.image.url for embed in embeds] == [f"attachment://{name}" for name in filenames]
    dumped = json.dumps([embed.to_dict() for embed in embeds], ensure_ascii=False)
    assert str(AUTHOR_ID) not in dumped and "小明" not in dumped and "student" not in dumped


def test_public_embed_without_images_is_a_plain_card() -> None:
    embeds = build_public_embeds(
        make_post(), number=3, gallery_url="https://example.com/c", filenames=[], published_at=discord.utils.utcnow()
    )
    assert len(embeds) == 1
    assert embeds[0].url is None and embeds[0].image.url is None


def test_thread_name_is_single_line_and_bounded() -> None:
    name = thread_name(12, CATEGORY.name, "第一行\n\n第二行   " + "很長" * 100)
    assert name.startswith("#12 😡 我要靠北｜第一行 第二行")
    assert len(name) == 100 and name.endswith("…") and "\n" not in name
    assert thread_name(12, CATEGORY.name, "   ") == "#12 😡 我要靠北"


def test_review_embed_shows_the_author_and_moderation() -> None:
    pending = build_review_embed(make_post(image_count=2), guild_id=GUILD_ID)
    fields = {field.name: field.value for field in pending.fields}
    assert f"<@{AUTHOR_ID}>" in fields["投稿者"] and str(AUTHOR_ID) in fields["投稿者"]
    assert fields["圖片"] == "2 張"
    assert "處理紀錄" not in fields
    assert pending.footer.text == "投稿 ID 7"

    published = build_review_embed(
        make_post(
            status=PostStatus.PUBLISHED,
            public_number=5,
            public_channel_id=PUBLIC_ID,
            public_message_id=999,
            moderated_at=1_700_000_100.0,
        ),
        guild_id=GUILD_ID,
    )
    fields = {field.name: field.value for field in published.fields}
    assert published.title.startswith("✅ 已發布 #5")
    assert f"/{GUILD_ID}/{PUBLIC_ID}/999" in fields["公開貼文"]
    assert "自動發布" in fields["處理紀錄"]

    removed = build_review_embed(
        make_post(status=PostStatus.REMOVED, public_number=5, moderator_id=MODERATOR_ID, moderator_reason="違規"),
        guild_id=GUILD_ID,
        note="注意事項",
    )
    fields = {field.name: field.value for field in removed.fields}
    assert f"<@{MODERATOR_ID}> 下架" in fields["處理紀錄"] and "理由：違規" in fields["處理紀錄"]
    assert fields["注意"] == "注意事項"


def test_sanitize_image_strips_metadata_and_keeps_orientation() -> None:
    raw = jpeg_with_metadata()
    with Image.open(io.BytesIO(raw)) as original:
        assert original.getexif().get_ifd(ExifTags.IFD.GPSInfo)
        assert "comment" in original.info

    cleaned, extension = sanitize_image(raw)

    assert extension == "jpg"
    with Image.open(io.BytesIO(cleaned)) as image:
        assert image.size == (20, 40)  # orientation baked into the pixels
        assert dict(image.getexif()) == {}
        assert "comment" not in image.info and "exif" not in image.info
    assert b"SecretCam" not in cleaned and b"alice" not in cleaned


def test_sanitize_image_rejects_non_images_and_unsupported_formats() -> None:
    bmp = io.BytesIO()
    Image.new("RGB", (4, 4)).save(bmp, "BMP")
    for data in (b"definitely not an image", bmp.getvalue()):
        with pytest.raises(ValueError):
            sanitize_image(data)


def test_sanitize_image_passes_animated_gif_through() -> None:
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    with Image.open(io.BytesIO(buffer.getvalue())) as image:
        assert image.is_animated
    assert sanitize_image(buffer.getvalue()) == (buffer.getvalue(), "gif")


def test_review_buttons_follow_post_state() -> None:
    async def run() -> None:
        pending = review_view(make_post())
        assert [child.item.custom_id for child in pending.children] == ["anon:approve:7", "anon:reject:7"]
        assert pending.is_persistent()
        published = review_view(make_post(status=PostStatus.PUBLISHED))
        assert [child.item.custom_id for child in published.children] == ["anon:takedown:7"]
        assert review_view(make_post(status=PostStatus.REJECTED)) is None
        assert review_view(make_post(status=PostStatus.REMOVED)) is None

        match = ReviewActionButton.__discord_ui_compiled_template__.fullmatch("anon:reject:42")
        button = await ReviewActionButton.from_custom_id(None, None, match)
        assert (button.action, button.post_id) == ("reject", 42)
        assert ReviewActionButton.__discord_ui_compiled_template__.fullmatch("anon:ban:42") is None

    asyncio.run(run())


def test_submission_modal_layout() -> None:
    async def run() -> None:
        categories = [CATEGORY, AnonymousCategory(2, "💌 我要告白")]
        payload = SubmissionModal(SimpleNamespace(), categories, require_review=True).to_dict()
        assert payload["title"] == "匿名投稿"
        assert [component["type"] for component in payload["components"]] == [10, 18, 18, 18]
        assert "審核" in payload["components"][0]["content"]
        select = payload["components"][1]["component"]
        assert [option["value"] for option in select["options"]] == ["1", "2"]
        upload = payload["components"][3]["component"]
        assert (upload["required"], upload["min_values"], upload["max_values"]) == (False, 0, 4)

        immediate = SubmissionModal(SimpleNamespace(), categories, require_review=False).to_dict()
        assert "立即發布" in immediate["components"][0]["content"]

    asyncio.run(run())


def test_submission_modal_passes_values_and_category_snapshot() -> None:
    async def run() -> None:
        cog = SimpleNamespace(handle_submission=AsyncMock())
        categories = [CATEGORY, AnonymousCategory(2, "💌 我要告白")]
        modal = SubmissionModal(cog, categories, require_review=True)
        attachment = FakeAttachment(b"x")
        modal._refresh(
            None,
            [
                {"type": 18, "component": {"type": 3, "custom_id": modal.category_select.custom_id, "values": ["2"]}},
                {"type": 18, "component": {"type": 4, "custom_id": modal.content_input.custom_id, "value": "內容"}},
                {"type": 18, "component": {"type": 19, "custom_id": modal.image_upload.custom_id, "values": ["999"]}},
            ],
            {ResolveKey(id="999", type=11): attachment},
        )
        interaction = make_interaction()
        await modal.on_submit(interaction)
        cog.handle_submission.assert_awaited_once_with(
            interaction, category=categories[1], content="內容", attachments=[attachment]
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Submission flows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("problem", ["unconfigured", "no_categories", "cooldown"])
def test_panel_refuses_without_opening_the_modal(tmp_path: Path, problem: str) -> None:
    async def run() -> None:
        board = await make_board(
            tmp_path,
            configured=problem != "unconfigured",
            categories=() if problem == "no_categories" else DEFAULT_CATEGORIES,
        )
        try:
            if problem == "cooldown":
                await submit(board)
            interaction = make_interaction()
            await board.cog.open_submission_modal(interaction)
            interaction.response.send_modal.assert_not_awaited()
            expected = {"unconfigured": "尚未設定", "no_categories": "沒有可用的分類", "cooldown": "秒後再試"}
            assert expected[problem] in last_reply(interaction)
            assert_private(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_panel_opens_the_modal(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            interaction = make_interaction()
            await board.cog.open_submission_modal(interaction)
            modal = interaction.response.send_modal.await_args.args[0]
            assert isinstance(modal, SubmissionModal)
            assert [c.name for c in modal.categories.values()] == list(DEFAULT_CATEGORIES)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_review_mode_sends_only_to_the_review_channel(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, require_review=True)
        try:
            interaction = await submit(board, attachments=[FakeAttachment(jpeg_with_metadata())])

            board.public.send.assert_not_awaited()
            board.review.send.assert_awaited_once()
            kwargs = board.review.send.await_args.kwargs
            post = await board.database.get_anonymous_post(1)
            assert post.status is PostStatus.PENDING and post.image_count == 1
            assert post.author_name == "小明 (@student)"
            assert post.review_message_id == REVIEW_ID * 1000 + 1
            assert [child.item.custom_id for child in kwargs["view"].children] == ["anon:approve:1", "anon:reject:1"]
            assert f"<@{AUTHOR_ID}>" in kwargs["embed"].fields[0].value
            assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
            assert [file.filename for file in kwargs["files"]] == ["anon_post1_1.jpg"]
            assert "審核通過後" in last_reply(interaction)
            assert_private(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_auto_mode_publishes_without_revealing_the_author(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, require_review=False)
        try:
            interaction = await submit(board, attachments=[FakeAttachment(jpeg_with_metadata())])

            board.public.send.assert_awaited_once()
            kwargs = board.public.send.await_args.kwargs
            embeds = kwargs["embeds"]
            files = kwargs["files"]
            assert embeds[0].title == "#1 😡 我要靠北"
            assert [file.filename for file in files] == ["anon_1_1.jpg"]
            assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
            with Image.open(files[0].fp) as image:
                assert dict(image.getexif()) == {}

            post = await board.database.get_anonymous_post(1)
            assert post.status is PostStatus.PUBLISHED and post.public_number == 1 and post.moderator_id is None

            # Anonymity regression: nothing that went to the public channel
            # carries the author's ID, names, or the original file name.
            public_payload = json.dumps(
                {
                    "embeds": [embed.to_dict() for embed in embeds],
                    "files": [file.filename for file in files],
                    "args": [str(arg) for arg in board.public.send.await_args.args],
                },
                ensure_ascii=False,
            )
            for secret in (str(AUTHOR_ID), "小明", "student", SECRET_FILENAME, "王小明"):
                assert secret not in public_payload

            review_card = board.bot.partials[(REVIEW_ID, post.review_message_id)]
            edit_kwargs = review_card.edit.await_args.kwargs
            assert [child.item.custom_id for child in edit_kwargs["view"].children] == ["anon:takedown:1"]
            assert "attachments" not in edit_kwargs  # keep the images on the card
            assert f"/{GUILD_ID}/{PUBLIC_ID}/{PUBLIC_ID * 1000 + 1}" in last_reply(interaction)
            assert_private(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_auto_mode_opens_a_comment_thread(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, require_review=False)
        sent: list[FakeMessage] = []
        original = board.public.send.side_effect

        async def capture(*args: object, **kwargs: object) -> FakeMessage:
            message = await original(*args, **kwargs)
            sent.append(message)
            return message

        board.public.send.side_effect = capture
        try:
            await submit(board)
            name = sent[0].create_thread.await_args.kwargs["name"]
            assert name.startswith("#1 😡 我要靠北｜今天午餐")
            assert str(AUTHOR_ID) not in name
        finally:
            await board.database.close()

    asyncio.run(run())


def test_auto_publish_failure_leaves_the_post_for_review(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, require_review=False)
        board.public.send.side_effect = http_error(discord.Forbidden, 403)
        try:
            interaction = await submit(board)
            post = await board.database.get_anonymous_post(1)
            assert post.status is PostStatus.PENDING and post.public_number is None
            edit_kwargs = board.bot.partials[(REVIEW_ID, post.review_message_id)].edit.await_args.kwargs
            assert "自動發布失敗" in edit_kwargs["embed"].fields[-1].value
            assert [child.item.custom_id for child in edit_kwargs["view"].children] == ["anon:approve:1", "anon:reject:1"]
            assert "轉交管理員" in last_reply(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_review_channel_failure_discards_the_post(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        board.review.send.side_effect = http_error(discord.Forbidden, 403)
        try:
            interaction = await submit(board)
            assert await board.database.get_anonymous_post(1) is None
            # The failed attempt must not cost the user their cooldown.
            assert await board.database.get_last_anonymous_post_at(AUTHOR_ID) is None
            assert "送出失敗" in last_reply(interaction)
            board.public.send.assert_not_awaited()
        finally:
            await board.database.close()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["too_large", "not_image", "too_many"])
def test_bad_uploads_are_refused_before_saving(tmp_path: Path, problem: str) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            if problem == "too_large":
                attachments = [FakeAttachment(jpeg_with_metadata(), size=50 * 1024 * 1024)]
            elif problem == "not_image":
                attachments = [FakeAttachment(b"%PDF-1.7 not an image", filename="report.pdf")]
            else:
                attachments = [FakeAttachment(jpeg_with_metadata()) for _ in range(5)]
            interaction = await submit(board, attachments=attachments)
            expected = {"too_large": "太大了", "not_image": "不是支援的圖片格式", "too_many": "最多只能附"}
            assert expected[problem] in last_reply(interaction)
            assert await board.database.get_anonymous_post(1) is None
            board.review.send.assert_not_awaited()
        finally:
            await board.database.close()

    asyncio.run(run())


def test_second_submission_within_cooldown_is_refused(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            await submit(board)
            interaction = await submit(board)
            assert "秒後再試" in last_reply(interaction)
            assert board.review.send.await_count == 1
        finally:
            await board.database.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Moderation flows
# ---------------------------------------------------------------------------


async def review_click(
    board: SimpleNamespace,
    action: str,
    *,
    post_id: int = 1,
    manage_messages: bool = True,
    attachments: list[object] | None = None,
) -> SimpleNamespace:
    post = await board.database.get_anonymous_post(post_id)
    message = FakeMessage(post.review_message_id, attachments)
    interaction = make_interaction(user_id=MODERATOR_ID, manage_messages=manage_messages, message=message)
    await board.cog.handle_review_click(interaction, action, post_id)
    return interaction


def test_concurrent_approvals_publish_once(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            await submit(board)
            results = await asyncio.gather(
                board.cog._approve(1, moderator_id=MODERATOR_ID, images=[]),
                board.cog._approve(1, moderator_id=MODERATOR_ID, images=[]),
            )
            assert board.public.send.await_count == 1
            assert sorted(post is None for post, _ in results) == [False, True]
            assert any("已經處理過了" in (error or "") for _, error in results)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_approve_button_publishes_images_and_notifies_author(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            await submit(board, attachments=[FakeAttachment(jpeg_with_metadata())])
            cleaned, _ = sanitize_image(jpeg_with_metadata())
            interaction = await review_click(
                board, "approve", attachments=[FakeAttachment(cleaned, filename="anon_post1_1.jpg")]
            )

            kwargs = board.public.send.await_args.kwargs
            assert [file.filename for file in kwargs["files"]] == ["anon_1_1.jpg"]
            post = await board.database.get_anonymous_post(1)
            assert post.status is PostStatus.PUBLISHED and post.moderator_id == MODERATOR_ID
            board.bot.create_dm.assert_awaited_once()
            assert board.bot.create_dm.await_args.args[0].id == AUTHOR_ID
            dm_text = board.bot.dm.send.await_args.args[0]
            assert "#1" in dm_text and f"/{PUBLIC_ID}/" in dm_text
            assert str(MODERATOR_ID) not in dm_text  # reviewers stay anonymous to authors
            assert "已私訊" in last_reply(interaction)
            assert_private(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_reject_asks_for_a_reason_then_notifies_author(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            await submit(board)
            click = await review_click(board, "reject")
            modal = click.response.send_modal.await_args.args[0]
            assert isinstance(modal, ReasonModal) and modal.action == "reject"

            board.bot.dm.send.side_effect = http_error(discord.Forbidden, 403)
            interaction = make_interaction(user_id=MODERATOR_ID, manage_messages=True)
            await board.cog.handle_reason_submit(interaction, "reject", 1, "人身攻擊")

            post = await board.database.get_anonymous_post(1)
            assert post.status is PostStatus.REJECTED and post.moderator_reason == "人身攻擊"
            card = board.bot.partials[(REVIEW_ID, post.review_message_id)]
            assert card.edit.await_args.kwargs["view"] is None
            assert "理由：人身攻擊" in board.bot.dm.send.await_args.args[0]
            # Blocked DMs are reported but do not undo the rejection.
            assert "無法私訊" in last_reply(interaction)
            board.public.send.assert_not_awaited()
            assert_private(interaction)

            # The approve button on a stale card is refused and the card redrawn.
            card.edit.reset_mock()
            stale = await review_click(board, "approve")
            assert "已經處理過了" in last_reply(stale)
            card.edit.assert_awaited_once()
            board.public.send.assert_not_awaited()
        finally:
            await board.database.close()

    asyncio.run(run())


@pytest.mark.parametrize("thread_exists", [True, False])
def test_takedown_deletes_thread_then_message(tmp_path: Path, thread_exists: bool) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, require_review=False)
        try:
            await submit(board)
            post = await board.database.get_anonymous_post(1)
            order: list[str] = []
            if thread_exists:
                thread = MagicMock(spec=discord.Thread)
                thread.delete = AsyncMock(side_effect=lambda **kwargs: order.append("thread"))
                board.bot.threads[post.public_message_id] = thread
            public_message = board.bot.partial(PUBLIC_ID, post.public_message_id)
            public_message.delete.side_effect = lambda: order.append("message")

            click = await review_click(board, "takedown")
            assert isinstance(click.response.send_modal.await_args.args[0], ReasonModal)
            interaction = make_interaction(user_id=MODERATOR_ID, manage_messages=True)
            await board.cog.handle_reason_submit(interaction, "takedown", 1, None)

            assert order == (["thread", "message"] if thread_exists else ["message"])
            removed = await board.database.get_anonymous_post(1)
            assert removed.status is PostStatus.REMOVED
            assert "#1" in board.bot.dm.send.await_args.args[0]
            assert "已下架 #1" in last_reply(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_takedown_failure_keeps_the_post_published(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, require_review=False)
        try:
            await submit(board)
            post = await board.database.get_anonymous_post(1)
            thread = MagicMock(spec=discord.Thread)
            thread.delete = AsyncMock(side_effect=http_error(discord.Forbidden, 403))
            board.bot.threads[post.public_message_id] = thread

            interaction = make_interaction(user_id=MODERATOR_ID, manage_messages=True)
            await board.cog.handle_reason_submit(interaction, "takedown", 1, None)

            assert (await board.database.get_anonymous_post(1)).status is PostStatus.PUBLISHED
            assert "管理討論串" in last_reply(interaction)
            board.bot.dm.send.assert_not_awaited()
        finally:
            await board.database.close()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["no_permission", "wrong_message"])
def test_review_click_is_refused(tmp_path: Path, problem: str) -> None:
    async def run() -> None:
        board = await make_board(tmp_path)
        try:
            await submit(board)
            if problem == "no_permission":
                interaction = await review_click(board, "approve", manage_messages=False)
                assert "管理訊息" in last_reply(interaction)
            else:
                interaction = make_interaction(
                    user_id=MODERATOR_ID, manage_messages=True, message=FakeMessage(123)
                )
                await board.cog.handle_review_click(interaction, "approve", 1)
                assert "找不到" in last_reply(interaction)
            board.public.send.assert_not_awaited()
            assert (await board.database.get_anonymous_post(1)).status is PostStatus.PENDING
        finally:
            await board.database.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Setup and admin commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("problem", ["type", "guild", "same", "everyone_sees_review", "missing_permission"])
def test_setup_rejects_unsafe_configuration(problem: str) -> None:
    async def run() -> None:
        database = SimpleNamespace(set_setting=AsyncMock(), get_setting=AsyncMock(return_value=None))
        cog = AnonymousBoardCog(FakeBot([]), database=database, guild_id=GUILD_ID)
        public = make_text_channel(PUBLIC_ID)
        review = make_text_channel(REVIEW_ID)
        expected = {
            "type": "文字頻道",
            "guild": "此伺服器",
            "same": "同一個",
            "everyone_sees_review": "@everyone",
            "missing_permission": "管理討論串",
        }[problem]
        if problem == "type":
            review = MagicMock(spec=discord.VoiceChannel)
        elif problem == "guild":
            review.guild.id = 999
        elif problem == "same":
            review = public
        elif problem == "everyone_sees_review":
            review = make_text_channel(REVIEW_ID, everyone_can_view=True)
        else:
            public = make_text_channel(
                PUBLIC_ID,
                bot_permissions=discord.Permissions(
                    view_channel=True, send_messages=True, embed_links=True,
                    attach_files=True, create_public_threads=True,
                ),
            )
        with pytest.raises(ValueError, match=expected):
            await cog.configure(public, review, require_review=True)
        database.set_setting.assert_not_awaited()

    asyncio.run(run())


def test_first_setup_seeds_default_categories_once(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, configured=False, categories=())
        try:
            assert await board.cog.configure(board.public, board.review, require_review=True) is True
            names = [c.name for c in await board.database.list_anonymous_categories()]
            assert names == list(DEFAULT_CATEGORIES)

            await board.database.remove_anonymous_category(DEFAULT_CATEGORIES[0])
            assert await board.cog.configure(board.public, board.review, require_review=False) is False
            assert await board.database.count_anonymous_categories() == len(DEFAULT_CATEGORIES) - 1
            assert await board.cog.get_config() == AnonymousBoardConfig(PUBLIC_ID, REVIEW_ID, False)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_setup_command_reports_privately(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, configured=False, categories=())
        try:
            interaction = make_interaction()
            await AnonymousBoardCog.anon_setup.callback(
                board.cog, interaction, board.public, board.review, False
            )
            reply = last_reply(interaction)
            assert "設定完成" in reply and "投稿立即發布" in reply and DEFAULT_CATEGORIES[0] in reply
            assert_private(interaction)
        finally:
            await board.database.close()

    asyncio.run(run())


def test_category_add_normalises_and_enforces_limits(tmp_path: Path) -> None:
    async def run() -> None:
        board = await make_board(tmp_path, categories=())
        try:
            add = AnonymousBoardCog.category_add.callback

            interaction = make_interaction()
            await add(board.cog, interaction, "  🎉   我要分享 ")
            assert "已新增分類「🎉 我要分享」" in last_reply(interaction)

            interaction = make_interaction()
            await add(board.cog, interaction, "🎉 我要分享")
            assert "已經存在" in last_reply(interaction)

            await board.database.seed_anonymous_categories(f"分類{i}" for i in range(MAX_CATEGORIES - 1))
            interaction = make_interaction()
            await add(board.cog, interaction, "第 26 個")
            assert f"最多 {MAX_CATEGORIES} 個" in last_reply(interaction)
            assert await board.database.count_anonymous_categories() == MAX_CATEGORIES
        finally:
            await board.database.close()

    asyncio.run(run())


def test_every_anon_command_is_admin_only() -> None:
    async def run() -> None:
        cog = AnonymousBoardCog(FakeBot([]), database=SimpleNamespace(), guild_id=GUILD_ID)
        group = cog.__cog_app_commands_group__
        assert group.guild_only
        commands = [cmd for cmd in group.walk_commands() if isinstance(cmd, discord.app_commands.Command)]
        assert {cmd.qualified_name for cmd in commands} == {
            "anon setup", "anon status", "anon send_panel",
            "anon category add", "anon category remove", "anon category list",
        }
        student = SimpleNamespace(user=SimpleNamespace(guild_permissions=discord.Permissions()))
        admin = SimpleNamespace(user=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True)))
        for command in commands:
            assert len(command.checks) == 1
            assert await command.checks[0](student) is False
            assert await command.checks[0](admin) is True

        interaction = make_interaction()
        await cog.cog_app_command_error(interaction, discord.app_commands.CheckFailure())
        assert "管理員" in last_reply(interaction)
        assert_private(interaction)

    asyncio.run(run())
