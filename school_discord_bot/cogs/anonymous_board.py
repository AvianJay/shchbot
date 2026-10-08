from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
import io
import logging
import math
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageOps

from school_discord_bot.cogs.announcements import admin_only
from school_discord_bot.db.database import Database
from school_discord_bot.models.anonymous_board import (
    DEFAULT_CATEGORIES,
    MAX_CATEGORIES,
    MAX_CATEGORY_NAME_LENGTH,
    MAX_CONTENT_LENGTH,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_REASON_LENGTH,
    SUBMIT_COOLDOWN_SECONDS,
    AnonymousBoardConfig,
    AnonymousCategory,
    AnonymousPost,
    PostStatus,
    collapse_whitespace,
    setting_key,
    thread_name,
)

logger = logging.getLogger(__name__)

_COLOR_PANEL = 0x5865F2   # Discord blurple
_COLOR_PUBLIC = 0x9B59B6  # purple — the public post card
_STATUS_COLORS: dict[PostStatus, int] = {
    PostStatus.PENDING: 0xFFA726,    # orange
    PostStatus.PUBLISHED: 0x43B581,  # green
    PostStatus.REJECTED: 0xED4245,   # red
    PostStatus.REMOVED: 0x78909C,    # blue-grey
}
_STATUS_LABELS: dict[PostStatus, str] = {
    PostStatus.PENDING: "📥 待審核",
    PostStatus.PUBLISHED: "✅ 已發布",
    PostStatus.REJECTED: "❌ 已拒絕",
    PostStatus.REMOVED: "🗑️ 已下架",
}

CUSTOM_SUBMIT = "anon:submit"

# action → (label, emoji, style)
_REVIEW_BUTTONS: dict[str, tuple[str, str, discord.ButtonStyle]] = {
    "approve": ("通過", "✅", discord.ButtonStyle.success),
    "reject": ("拒絕", "❌", discord.ButtonStyle.danger),
    "takedown": ("下架", "🗑️", discord.ButtonStyle.secondary),
}

# Pillow format → (extension, format to re-save as). MPO is how Pillow opens
# multi-picture phone JPEGs; only its primary picture is kept.
_IMAGE_FORMATS: dict[str, tuple[str, str]] = {
    "JPEG": ("jpg", "JPEG"),
    "MPO": ("jpg", "JPEG"),
    "PNG": ("png", "PNG"),
    "WEBP": ("webp", "WEBP"),
    "GIF": ("gif", "GIF"),
}
_ANIMATED_PASSTHROUGH_FORMATS = frozenset({"GIF", "PNG", "WEBP"})

_GENERIC_ERROR = "❌ 發生未預期的錯誤，請稍後再試或聯絡管理員。"
_NOT_CONFIGURED = "❌ 匿名版尚未設定，請聯絡管理員。"
_ADMIN_ONLY = "❌ 此指令限具有「管理伺服器」或「管理頻道」權限的管理員使用。"

_PUBLIC_CHANNEL_PERMISSIONS: dict[str, str] = {
    "view_channel": "檢視頻道",
    "send_messages": "發送訊息",
    "embed_links": "嵌入連結",
    "attach_files": "附加檔案",
    "create_public_threads": "建立公開討論串",
    "manage_threads": "管理討論串",
}
_REVIEW_CHANNEL_PERMISSIONS: dict[str, str] = {
    "view_channel": "檢視頻道",
    "send_messages": "發送訊息",
    "embed_links": "嵌入連結",
    "attach_files": "附加檔案",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


async def _reply(interaction: discord.Interaction, message: str) -> None:
    """Answer privately whether or not the interaction was already acknowledged."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def sanitize_image(data: bytes) -> tuple[bytes, str]:
    """Strip metadata from an uploaded image; return ``(bytes, extension)``.

    Photos can carry EXIF GPS coordinates, camera serials or a JPEG comment
    naming the owner, any of which would unmask an anonymous poster. Still
    images are re-encoded from their pixels alone, after applying the EXIF
    orientation so dropping it does not leave the picture sideways. Animated
    GIF/PNG/WEBP cannot be re-encoded faithfully this way and pass through.

    Raises ``ValueError`` when the data is not a supported image.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            source_format = image.format or ""
            if source_format not in _IMAGE_FORMATS:
                raise ValueError(f"unsupported image format: {source_format or 'unknown'}")
            extension, save_format = _IMAGE_FORMATS[source_format]
            if source_format in _ANIMATED_PASSTHROUGH_FORMATS and getattr(image, "is_animated", False):
                return data, extension

            # Colour profiles hold no personal data and keep phone photos accurate.
            icc_profile = image.info.get("icc_profile")
            cleaned = ImageOps.exif_transpose(image)
            cleaned.info = {}
            if save_format == "JPEG" and cleaned.mode not in ("RGB", "L", "CMYK"):
                cleaned = cleaned.convert("RGB")
            options: dict[str, Any] = {"icc_profile": icc_profile} if icc_profile else {}
            if save_format == "JPEG":
                options["quality"] = 90
            output = io.BytesIO()
            cleaned.save(output, format=save_format, **options)
    except ValueError:
        raise
    except Exception as exc:  # Pillow raises many types on malformed input.
        raise ValueError("not a readable image") from exc
    return output.getvalue(), extension


def build_public_embeds(
    post: AnonymousPost,
    *,
    number: int,
    gallery_url: str,
    filenames: list[str],
    published_at: datetime,
) -> list[discord.Embed]:
    """Build the public post card: a text embed plus one embed per extra image.

    Discord merges embeds sharing a ``url`` into a single card with a grid of
    up to four images. Images referenced via ``attachment://`` are not shown
    again as loose attachments, which would otherwise render above the text.
    Nothing here may identify the author.
    """
    embed = discord.Embed(
        title=f"#{number} {post.category_name}",
        description=discord.utils.escape_mentions(post.content),
        color=_COLOR_PUBLIC,
        timestamp=published_at,
    )
    embed.set_footer(text="匿名投稿")
    if not filenames:
        return [embed]

    embed.url = gallery_url
    embed.set_image(url=f"attachment://{filenames[0]}")
    embeds = [embed]
    for filename in filenames[1:]:
        extra = discord.Embed(url=gallery_url)
        extra.set_image(url=f"attachment://{filename}")
        embeds.append(extra)
    return embeds


def public_jump_url(post: AnonymousPost, guild_id: int) -> str | None:
    if post.public_channel_id is None or post.public_message_id is None:
        return None
    return f"https://discord.com/channels/{guild_id}/{post.public_channel_id}/{post.public_message_id}"


def _moderation_record(post: AnonymousPost) -> str | None:
    if post.status is PostStatus.PENDING:
        return None
    if post.moderator_id is None:
        record = "🤖 未開啟審核，自動發布"
    else:
        verb = {
            PostStatus.PUBLISHED: "通過",
            PostStatus.REJECTED: "拒絕",
            PostStatus.REMOVED: "下架",
        }[post.status]
        record = f"<@{post.moderator_id}> {verb}"
    if post.moderated_at is not None:
        record += f"（<t:{int(post.moderated_at)}:f>）"
    if post.moderator_reason:
        record += f"\n理由：{post.moderator_reason}"
    return record


def build_review_embed(
    post: AnonymousPost,
    *,
    guild_id: int,
    note: str | None = None,
) -> discord.Embed:
    """Build the staff-only card. Unlike the public post it shows the author."""
    status = _STATUS_LABELS[post.status]
    if post.public_number is not None:
        status = f"{status} #{post.public_number}"
    embed = discord.Embed(
        title=f"{status}｜{post.category_name}",
        description=post.content,
        color=_STATUS_COLORS[post.status],
        timestamp=datetime.fromtimestamp(post.created_at, tz=UTC),
    )
    embed.add_field(
        name="投稿者",
        value=(
            f"<@{post.author_id}> {discord.utils.escape_markdown(post.author_name)}\n"
            f"ID：{post.author_id}"
        ),
        inline=False,
    )
    if post.image_count:
        embed.add_field(name="圖片", value=f"{post.image_count} 張", inline=True)
    jump_url = public_jump_url(post, guild_id)
    if post.status is PostStatus.PUBLISHED and jump_url:
        embed.add_field(name="公開貼文", value=f"[前往 #{post.public_number}]({jump_url})", inline=True)
    record = _moderation_record(post)
    if record:
        embed.add_field(name="處理紀錄", value=record, inline=False)
    if note:
        embed.add_field(name="注意", value=note, inline=False)
    embed.set_footer(text=f"投稿 ID {post.id}")
    return embed


def _excerpt(content: str, limit: int = 100) -> str:
    text = collapse_whitespace(content)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dm_note(delivered: bool) -> str:
    return "📨 已私訊通知投稿者。" if delivered else "⚠️ 無法私訊投稿者（對方可能關閉了私訊）。"


# ---------------------------------------------------------------------------
# Review buttons (persistent across restarts via DynamicItem)
# ---------------------------------------------------------------------------


class ReviewActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"anon:(?P<action>approve|reject|takedown):(?P<post_id>\d+)",
):
    """Approve / reject / take-down button on a review-channel card.

    The post ID is encoded in the ``custom_id`` so the button keeps working
    after a restart without any stored view state.
    """

    def __init__(self, action: str, post_id: int) -> None:
        label, emoji, style = _REVIEW_BUTTONS[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"anon:{action}:{post_id}",
            )
        )
        self.action = action
        self.post_id = post_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "ReviewActionButton":
        return cls(match["action"], int(match["post_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("AnonymousBoardCog")
        if not isinstance(cog, AnonymousBoardCog):
            await interaction.response.send_message("❌ 匿名版模組尚未載入", ephemeral=True)
            return
        # Errors raised from a DynamicItem are only logged by discord.py, which
        # would leave the reviewer staring at a spinner.
        try:
            await cog.handle_review_click(interaction, self.action, self.post_id)
        except Exception:
            logger.exception("Anonymous post %s: %s action failed", self.post_id, self.action)
            await _reply(interaction, _GENERIC_ERROR)


def review_view(post: AnonymousPost) -> discord.ui.View | None:
    """Buttons for a review card in the post's current state."""
    if post.status is PostStatus.PENDING:
        actions: tuple[str, ...] = ("approve", "reject")
    elif post.status is PostStatus.PUBLISHED:
        actions = ("takedown",)
    else:
        return None
    view = discord.ui.View(timeout=None)
    for action in actions:
        view.add_item(ReviewActionButton(action, post.id))
    return view


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


class SubmissionModal(discord.ui.Modal, title="匿名投稿"):
    """The submission form. A fresh instance is built for every click."""

    def __init__(
        self,
        cog: "AnonymousBoardCog",
        categories: list[AnonymousCategory],
        *,
        require_review: bool,
    ) -> None:
        # A submit after the modal times out is silently dropped, so be generous.
        super().__init__(timeout=1800)
        self.cog = cog
        # Snapshot the categories so one deleted while the user is typing does
        # not cost them what they wrote.
        self.categories = {str(category.id): category for category in categories}

        notice = [
            "🔒 其他同學看不到投稿者；管理員可以看到投稿者，以便處理違規內容。",
            "⚠️ 請勿人身攻擊、洩漏他人個資或散布不實指控。",
            "📝 投稿需經管理員審核後才會發布。" if require_review else "📝 送出後會立即發布。",
        ]
        self.add_item(discord.ui.TextDisplay("\n".join(notice)))

        self.category_select = discord.ui.Select(
            placeholder="選擇分類",
            options=[
                discord.SelectOption(label=category.name, value=str(category.id))
                for category in categories
            ],
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(discord.ui.Label(text="分類", component=self.category_select))

        self.content_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="想說什麼？",
            min_length=1,
            max_length=MAX_CONTENT_LENGTH,
            required=True,
        )
        self.add_item(discord.ui.Label(text="內容", component=self.content_input))

        self.image_upload = discord.ui.FileUpload(
            required=False,
            min_values=0,
            max_values=MAX_IMAGES,
        )
        self.add_item(
            discord.ui.Label(
                text="圖片（選填）",
                description=f"最多 {MAX_IMAGES} 張，位置等中繼資料會自動移除",
                component=self.image_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        selected = self.category_select.values
        category = self.categories.get(selected[0]) if selected else None
        if category is None:
            await interaction.response.send_message("❌ 請選擇分類。", ephemeral=True)
            return
        await self.cog.handle_submission(
            interaction,
            category=category,
            content=self.content_input.value,
            attachments=list(self.image_upload.values),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("Anonymous submission failed", exc_info=error)
        await _reply(interaction, _GENERIC_ERROR)


class ReasonModal(discord.ui.Modal):
    """Optional reason for a rejection or takedown, relayed to the author."""

    def __init__(self, cog: "AnonymousBoardCog", action: str, post_id: int) -> None:
        super().__init__(title="拒絕投稿" if action == "reject" else "下架貼文", timeout=600)
        self.cog = cog
        self.action = action
        self.post_id = post_id
        self.reason_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="會私訊告知投稿者，不會透露是誰處理的",
            required=False,
            max_length=MAX_REASON_LENGTH,
        )
        self.add_item(discord.ui.Label(text="理由（選填）", component=self.reason_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason_input.value.strip() or None
        await self.cog.handle_reason_submit(interaction, self.action, self.post_id, reason)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("Anonymous post %s: %s failed", self.post_id, self.action, exc_info=error)
        await _reply(interaction, _GENERIC_ERROR)


# ---------------------------------------------------------------------------
# Submission button (on the panel and under every published post)
# ---------------------------------------------------------------------------


class SubmitButton(discord.ui.DynamicItem[discord.ui.Button], template=r"anon:submit"):
    """Opens the submission modal.

    A DynamicItem rather than a plain persistent button because it goes under
    every published post: discord.py keeps a view object per message for each
    static persistent view it sends, which would pile up one per post, while
    dynamic items are matched by ``custom_id`` alone and retain nothing.
    """

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="匿名投稿",
                emoji="✍️",
                custom_id=CUSTOM_SUBMIT,
                style=discord.ButtonStyle.primary,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "SubmitButton":
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("AnonymousBoardCog")
        if not isinstance(cog, AnonymousBoardCog):
            await interaction.response.send_message("❌ 匿名版模組尚未載入", ephemeral=True)
            return
        # Errors raised from a DynamicItem are only logged by discord.py.
        try:
            await cog.open_submission_modal(interaction)
        except Exception:
            logger.exception("Anonymous board submit button failed")
            await _reply(interaction, _GENERIC_ERROR)


class AnonymousPanelView(discord.ui.View):
    """The submission button, as sent on the panel and under each post."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(SubmitButton())


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


@app_commands.guild_only()
class AnonymousBoardCog(
    commands.GroupCog,
    group_name="anon",
    group_description="匿名版管理指令",
):
    """Anonymous posting board with optional staff review."""

    category = app_commands.Group(name="category", description="管理匿名版分類")

    def __init__(self, bot: commands.Bot, *, database: Database, guild_id: int) -> None:
        self.bot = bot
        self.database = database
        self.guild_id = guild_id
        self.logger = logging.getLogger(f"{__name__}.AnonymousBoardCog")
        # Serialises every post state change: two reviewers clicking at once,
        # approve racing reject, or a manual click racing auto-publish.
        self._moderation_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def get_config(self) -> AnonymousBoardConfig | None:
        stored = await self.database.get_setting(setting_key(self.guild_id))
        return AnonymousBoardConfig.from_json(stored) if stored else None

    def _validate_channels(self, public: object, review: object) -> None:
        if not isinstance(public, discord.TextChannel) or not isinstance(review, discord.TextChannel):
            raise ValueError("請選擇文字頻道。")
        if public.guild.id != self.guild_id or review.guild.id != self.guild_id:
            raise ValueError("請選擇此伺服器的頻道。")
        if public.id == review.id:
            raise ValueError("匿名頻道與後台頻道不能是同一個頻道。")
        # The review channel shows who wrote every post.
        if review.permissions_for(review.guild.default_role).view_channel:
            raise ValueError(
                f"後台頻道 {review.mention} 會顯示投稿者身分，"
                "請先關閉 @everyone 的「檢視頻道」權限，只開放給管理員。"
            )
        me = review.guild.me
        if me is None:
            raise ValueError("無法取得機器人的伺服器成員資料。")
        for channel, required in (
            (public, _PUBLIC_CHANNEL_PERMISSIONS),
            (review, _REVIEW_CHANNEL_PERMISSIONS),
        ):
            permissions = channel.permissions_for(me)
            missing = [label for name, label in required.items() if not getattr(permissions, name)]
            if missing:
                raise ValueError(f"機器人在 {channel.mention} 缺少權限：{'、'.join(missing)}。")

    async def configure(
        self,
        public: discord.TextChannel,
        review: discord.TextChannel,
        *,
        require_review: bool,
    ) -> bool:
        """Validate and save the settings; return True if default categories were created."""
        self._validate_channels(public, review)
        current = await self.get_config()
        await self.database.set_setting(
            setting_key(self.guild_id),
            AnonymousBoardConfig(
                public.id,
                review.id,
                require_review,
                # Re-running setup keeps the notification role set by /anon notify.
                notify_role_id=current.notify_role_id if current else None,
            ).to_json(),
        )
        if current is None and await self.database.count_anonymous_categories() == 0:
            await self.database.seed_anonymous_categories(DEFAULT_CATEGORIES)
            return True
        return False

    def _validate_notify_role(self, role: discord.Role, config: AnonymousBoardConfig) -> None:
        if role.guild.id != self.guild_id:
            raise ValueError("請選擇此伺服器的身分組。")
        if role.is_default():
            raise ValueError("不能選擇 @everyone，請建立一個專用的通知身分組。")
        if role.mentionable:
            return
        # An unmentionable role is only pinged if the bot may mention all roles;
        # otherwise the mention renders but nobody is notified.
        me = role.guild.me
        channel = self.bot.get_channel(config.public_channel_id)
        if isinstance(channel, discord.TextChannel):
            can_mention_all = channel.permissions_for(me).mention_everyone
        else:
            can_mention_all = me.guild_permissions.mention_everyone
        if not can_mention_all:
            raise ValueError(
                f"身分組「{role.name}」不允許被提及。請在身分組設定開啟「允許任何人 @提及此身分組」，"
                "或給機器人匿名頻道的「提及 @everyone、@here 和所有身分組」權限。"
            )

    async def set_notify_role(self, role: discord.Role | None) -> None:
        """Set the role mentioned on each published post, or clear it with None."""
        config = await self.get_config()
        if config is None:
            raise ValueError("匿名版尚未設定，請先執行 /anon setup。")
        if role is not None:
            self._validate_notify_role(role, config)
        await self.database.set_setting(
            setting_key(self.guild_id),
            dataclasses.replace(config, notify_role_id=role.id if role else None).to_json(),
        )

    def _publish_mention(self, config: AnonymousBoardConfig) -> tuple[str | None, discord.AllowedMentions]:
        """Message content and mention policy for a public post.

        Only the configured role can ever be pinged; the post text sits in an
        embed and never pings anyone.
        """
        if config.notify_role_id is None:
            return None, discord.AllowedMentions.none()
        guild = self.bot.get_guild(self.guild_id)
        if guild is None or guild.get_role(config.notify_role_id) is None:
            self.logger.warning(
                "Anonymous board notification role %s no longer exists; publishing without it",
                config.notify_role_id,
            )
            return None, discord.AllowedMentions.none()
        return f"<@&{config.notify_role_id}>", discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(id=config.notify_role_id)],
            replied_user=False,
        )

    async def _resolve_text_channel(self, channel_id: int) -> discord.TextChannel:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError(f"channel {channel_id} is not a text channel")
        return channel

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    async def _cooldown_remaining(self, author_id: int, *, now: float | None = None) -> int:
        last = await self.database.get_last_anonymous_post_at(author_id)
        if last is None:
            return 0
        current = time.time() if now is None else now
        return max(0, math.ceil(last + SUBMIT_COOLDOWN_SECONDS - current))

    async def open_submission_modal(self, interaction: discord.Interaction) -> None:
        config = await self.get_config()
        if config is None:
            await interaction.response.send_message(_NOT_CONFIGURED, ephemeral=True)
            return
        categories = await self.database.list_anonymous_categories()
        if not categories:
            await interaction.response.send_message(
                "❌ 匿名版目前沒有可用的分類，請聯絡管理員。",
                ephemeral=True,
            )
            return
        remaining = await self._cooldown_remaining(interaction.user.id)
        if remaining > 0:
            await interaction.response.send_message(
                f"⏰ 投稿太頻繁了，請在 {remaining} 秒後再試。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            SubmissionModal(self, categories, require_review=config.require_review)
        )

    async def _prepare_images(
        self,
        attachments: list[discord.Attachment],
        *,
        size_limit: int,
    ) -> list[tuple[bytes, str]]:
        """Download, check and strip metadata from uploaded images.

        Raises ``ValueError`` with a message meant for the submitter.
        """
        if len(attachments) > MAX_IMAGES:
            raise ValueError(f"最多只能附 {MAX_IMAGES} 張圖片。")
        limit_text = f"{size_limit / (1024 * 1024):g} MB"
        images: list[tuple[bytes, str]] = []
        for attachment in attachments:
            if attachment.size > size_limit:
                raise ValueError(f"「{attachment.filename}」太大了，每張圖片上限 {limit_text}。")
            try:
                data = await attachment.read()
            except discord.HTTPException:
                raise ValueError(f"無法讀取「{attachment.filename}」，請重新上傳。") from None
            try:
                cleaned, extension = await asyncio.to_thread(sanitize_image, data)
            except ValueError:
                raise ValueError(
                    f"「{attachment.filename}」不是支援的圖片格式（JPG、PNG、GIF、WEBP）。"
                ) from None
            if len(cleaned) > size_limit:
                raise ValueError(f"「{attachment.filename}」太大了，每張圖片上限 {limit_text}。")
            images.append((cleaned, extension))
        return images

    async def handle_submission(
        self,
        interaction: discord.Interaction,
        *,
        category: AnonymousCategory,
        content: str,
        attachments: list[discord.Attachment],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        content = content.strip()
        if not content:
            await interaction.followup.send("❌ 內容不能是空白。", ephemeral=True)
            return
        config = await self.get_config()
        if config is None:
            await interaction.followup.send(_NOT_CONFIGURED, ephemeral=True)
            return
        # Cheap early exit before downloading images; the insert below is the
        # authoritative check.
        remaining = await self._cooldown_remaining(interaction.user.id)
        if remaining > 0:
            await interaction.followup.send(f"⏰ 投稿太頻繁了，請在 {remaining} 秒後再試。", ephemeral=True)
            return

        try:
            images = await self._prepare_images(
                attachments,
                size_limit=min(interaction.filesize_limit, MAX_IMAGE_BYTES),
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        now = time.time()
        user = interaction.user
        post_id = await self.database.create_anonymous_post(
            author_id=user.id,
            author_name=f"{user.display_name} (@{user.name})",
            category_name=category.name,
            content=content,
            image_count=len(images),
            created_at=now,
            cooldown_seconds=SUBMIT_COOLDOWN_SECONDS,
        )
        if post_id is None:
            remaining = await self._cooldown_remaining(user.id, now=now)
            await interaction.followup.send(f"⏰ 投稿太頻繁了，請在 {remaining} 秒後再試。", ephemeral=True)
            return

        # Every post reaches the review channel first, even when review is off,
        # so each public post has an identity record and a takedown button.
        try:
            await self._send_for_review(post_id, config, images)
        except (discord.HTTPException, ValueError) as exc:
            self.logger.exception("Failed to deliver anonymous post %s to the review channel", post_id)
            await self.database.delete_anonymous_post(post_id)
            if isinstance(exc, discord.HTTPException) and exc.status == 413:
                message = "❌ 圖片總大小超過 Discord 上限，請減少張數或縮小圖片後再試。"
            else:
                message = "❌ 投稿送出失敗，請稍後再試或聯絡管理員。"
            await interaction.followup.send(message, ephemeral=True)
            return

        if config.require_review:
            await interaction.followup.send(
                "✅ 投稿已送出，管理員審核通過後就會發布。\n審核結果會以私訊通知你。",
                ephemeral=True,
            )
            return

        published, error = await self._approve(post_id, moderator_id=None, images=images)
        if published is None:
            post = await self.database.get_anonymous_post(post_id)
            if post is not None and post.status is PostStatus.PENDING:
                await self._refresh_review_message(post, note=f"⚠️ 自動發布失敗：{error}")
            await interaction.followup.send(
                "⚠️ 投稿已收到，但自動發布失敗，已轉交管理員處理。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 投稿已發布為 #{published.public_number}：{public_jump_url(published, self.guild_id)}",
            ephemeral=True,
        )

    async def _send_for_review(
        self,
        post_id: int,
        config: AnonymousBoardConfig,
        images: list[tuple[bytes, str]],
    ) -> None:
        post = await self.database.get_anonymous_post(post_id)
        if post is None:
            raise ValueError(f"anonymous post {post_id} vanished")
        channel = await self._resolve_text_channel(config.review_channel_id)
        kwargs: dict[str, Any] = {}
        if images:
            kwargs["files"] = [
                discord.File(io.BytesIO(data), filename=f"anon_post{post_id}_{index}.{extension}")
                for index, (data, extension) in enumerate(images, start=1)
            ]
        message = await channel.send(
            embed=build_review_embed(post, guild_id=self.guild_id),
            view=review_view(post),
            allowed_mentions=discord.AllowedMentions.none(),
            **kwargs,
        )
        await self.database.set_anonymous_post_review_message(
            post_id,
            channel_id=channel.id,
            message_id=message.id,
        )

    # ------------------------------------------------------------------
    # Moderation
    # ------------------------------------------------------------------

    async def _approve(
        self,
        post_id: int,
        *,
        moderator_id: int | None,
        images: list[tuple[bytes, str]],
    ) -> tuple[AnonymousPost | None, str | None]:
        """Publish a pending post. Returns ``(post, None)`` or ``(None, error)``."""
        async with self._moderation_lock:
            post = await self.database.get_anonymous_post(post_id)
            if post is None:
                return None, "找不到這則投稿。"
            if post.status is not PostStatus.PENDING:
                return None, f"這則投稿已經處理過了（{_STATUS_LABELS[post.status]}）。"
            config = await self.get_config()
            if config is None:
                return None, "匿名版尚未設定。"
            try:
                channel = await self._resolve_text_channel(config.public_channel_id)
            except (discord.HTTPException, ValueError):
                self.logger.exception("Anonymous board channel %s is unavailable", config.public_channel_id)
                return None, "找不到匿名頻道，請重新執行 /anon setup。"

            number = await self.database.next_anonymous_public_number()
            filenames = [
                f"anon_{number}_{index}.{extension}"
                for index, (_, extension) in enumerate(images, start=1)
            ]
            now = time.time()
            kwargs: dict[str, Any] = {}
            if images:
                kwargs["files"] = [
                    discord.File(io.BytesIO(data), filename=filename)
                    for (data, _), filename in zip(images, filenames)
                ]
            mention, allowed_mentions = self._publish_mention(config)
            try:
                message = await channel.send(
                    content=mention,
                    embeds=build_public_embeds(
                        post,
                        number=number,
                        gallery_url=channel.jump_url,
                        filenames=filenames,
                        published_at=datetime.fromtimestamp(now, tz=UTC),
                    ),
                    view=AnonymousPanelView(),
                    allowed_mentions=allowed_mentions,
                    **kwargs,
                )
            except discord.HTTPException:
                self.logger.exception("Failed to publish anonymous post %s", post_id)
                return None, "發布到匿名頻道失敗，請確認機器人的頻道權限後再試。"

            # Recorded before anything else: if the bot died between the send
            # above and this write, the post would stay pending and a second
            # approval would publish it twice.
            await self.database.mark_anonymous_post_published(
                post_id,
                public_number=number,
                channel_id=channel.id,
                message_id=message.id,
                moderator_id=moderator_id,
                moderated_at=now,
            )

            try:
                await message.create_thread(name=thread_name(number, post.category_name, post.content))
            except discord.HTTPException:
                self.logger.warning("Failed to open a thread for anonymous post #%s", number, exc_info=True)

            published = await self.database.get_anonymous_post(post_id)
            if published is not None:
                await self._refresh_review_message(published)
            return published, None

    async def _reject(
        self,
        post_id: int,
        *,
        moderator_id: int,
        reason: str | None,
    ) -> tuple[AnonymousPost | None, str | None]:
        async with self._moderation_lock:
            post = await self.database.get_anonymous_post(post_id)
            if post is None:
                return None, "找不到這則投稿。"
            if post.status is not PostStatus.PENDING:
                return None, f"這則投稿已經處理過了（{_STATUS_LABELS[post.status]}）。"
            await self.database.mark_anonymous_post_rejected(
                post_id,
                moderator_id=moderator_id,
                reason=reason,
                moderated_at=time.time(),
            )
            rejected = await self.database.get_anonymous_post(post_id)
            if rejected is not None:
                await self._refresh_review_message(rejected)
            return rejected, None

    async def _takedown(
        self,
        post_id: int,
        *,
        moderator_id: int,
        reason: str | None,
    ) -> tuple[AnonymousPost | None, str | None]:
        async with self._moderation_lock:
            post = await self.database.get_anonymous_post(post_id)
            if post is None:
                return None, "找不到這則投稿。"
            if post.status is not PostStatus.PUBLISHED:
                return None, f"這則投稿目前無法下架（{_STATUS_LABELS[post.status]}）。"
            try:
                await self._delete_public_post(post)
            except discord.HTTPException:
                self.logger.exception("Failed to take down anonymous post #%s", post.public_number)
                return None, "刪除公開貼文失敗，請確認機器人在匿名頻道有「管理討論串」權限。"
            await self.database.mark_anonymous_post_removed(
                post_id,
                moderator_id=moderator_id,
                reason=reason,
                moderated_at=time.time(),
            )
            removed = await self.database.get_anonymous_post(post_id)
            if removed is not None:
                await self._refresh_review_message(removed)
            return removed, None

    async def _delete_public_post(self, post: AnonymousPost) -> None:
        if post.public_channel_id is None or post.public_message_id is None:
            return
        # A thread started from a message shares that message's ID. Deleting
        # only the message would leave the comment thread behind, so it goes
        # first. Anything already gone is fine — retrying must be safe.
        guild = self.bot.get_guild(self.guild_id)
        thread = guild.get_thread(post.public_message_id) if guild is not None else None
        try:
            if thread is None:
                thread = await self.bot.fetch_channel(post.public_message_id)
            if isinstance(thread, discord.Thread):
                await thread.delete(reason="匿名版貼文下架")
        except discord.NotFound:
            pass
        try:
            await self.bot.get_partial_messageable(post.public_channel_id).get_partial_message(
                post.public_message_id
            ).delete()
        except discord.NotFound:
            pass

    async def _refresh_review_message(self, post: AnonymousPost, *, note: str | None = None) -> None:
        """Redraw a review card from the database row.

        ``attachments`` is deliberately not passed, so the images stay attached.
        """
        if post.review_channel_id is None or post.review_message_id is None:
            return
        message = self.bot.get_partial_messageable(post.review_channel_id).get_partial_message(
            post.review_message_id
        )
        try:
            await message.edit(
                embed=build_review_embed(post, guild_id=self.guild_id, note=note),
                view=review_view(post),
            )
        except discord.HTTPException:
            self.logger.warning("Failed to update the review card for anonymous post %s", post.id, exc_info=True)

    async def _notify_author(self, author_id: int, text: str) -> bool:
        try:
            channel = await self.bot.create_dm(discord.Object(id=author_id))
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return False
        return True

    async def _images_from_message(self, message: discord.Message) -> list[tuple[bytes, str]]:
        """Re-download a review card's images (already stripped when first uploaded)."""
        images: list[tuple[bytes, str]] = []
        for attachment in message.attachments:
            extension = attachment.filename.rsplit(".", 1)[-1].lower()
            images.append((await attachment.read(), extension))
        return images

    async def handle_review_click(
        self,
        interaction: discord.Interaction,
        action: str,
        post_id: int,
    ) -> None:
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message(
                "❌ 你需要此頻道的「管理訊息」權限才能審核投稿。",
                ephemeral=True,
            )
            return
        post = await self.database.get_anonymous_post(post_id)
        if (
            post is None
            or interaction.message is None
            or post.review_message_id != interaction.message.id
        ):
            await interaction.response.send_message("❌ 找不到這則投稿。", ephemeral=True)
            return
        expected = PostStatus.PUBLISHED if action == "takedown" else PostStatus.PENDING
        if post.status is not expected:
            await interaction.response.send_message(
                f"❌ 這則投稿已經處理過了（{_STATUS_LABELS[post.status]}）。",
                ephemeral=True,
            )
            # The buttons are stale (another reviewer got there first); redraw.
            await self._refresh_review_message(post)
            return
        if action != "approve":
            await interaction.response.send_modal(ReasonModal(self, action, post_id))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            images = await self._images_from_message(interaction.message)
        except discord.HTTPException:
            self.logger.exception("Failed to download images of anonymous post %s", post_id)
            await interaction.followup.send("❌ 無法取得投稿附帶的圖片，請稍後再試。", ephemeral=True)
            return
        published, error = await self._approve(post_id, moderator_id=interaction.user.id, images=images)
        if published is None:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        delivered = await self._notify_author(
            published.author_id,
            f"✅ 你在「{published.category_name}」的匿名投稿已通過審核，"
            f"發布為 #{published.public_number}：\n{public_jump_url(published, self.guild_id)}",
        )
        await interaction.followup.send(
            f"✅ 已發布為 #{published.public_number}。{_dm_note(delivered)}",
            ephemeral=True,
        )

    async def handle_reason_submit(
        self,
        interaction: discord.Interaction,
        action: str,
        post_id: int,
        reason: str | None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        reason_text = reason or "未提供"
        if action == "reject":
            post, error = await self._reject(post_id, moderator_id=interaction.user.id, reason=reason)
            if post is None:
                await interaction.followup.send(f"❌ {error}", ephemeral=True)
                return
            delivered = await self._notify_author(
                post.author_id,
                f"❌ 你在「{post.category_name}」的匿名投稿未通過審核。\n"
                f"理由：{reason_text}\n\n> {_excerpt(post.content)}",
            )
            await interaction.followup.send(f"✅ 已拒絕這則投稿。{_dm_note(delivered)}", ephemeral=True)
            return

        post, error = await self._takedown(post_id, moderator_id=interaction.user.id, reason=reason)
        if post is None:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        delivered = await self._notify_author(
            post.author_id,
            f"🗑️ 你的匿名投稿 #{post.public_number}（{post.category_name}）已被管理員下架。\n"
            f"理由：{reason_text}",
        )
        await interaction.followup.send(
            f"✅ 已下架 #{post.public_number}。{_dm_note(delivered)}",
            ephemeral=True,
        )

    async def post_panel(self, channel: discord.TextChannel) -> None:
        """Post the persistent submission panel to a channel."""
        embed = discord.Embed(
            title="📮 匿名投稿",
            description=(
                "有話想說又不想具名？點擊下方「**匿名投稿**」按鈕，選擇分類並寫下內容即可。\n\n"
                "**投稿須知：**\n"
                "• 其他同學看不到投稿者；管理員可以看到投稿者，以便處理違規內容\n"
                "• 請勿人身攻擊、洩漏他人個資或散布不實指控\n"
                f"• 可附最多 {MAX_IMAGES} 張圖片，位置等中繼資料會自動移除\n"
                f"• 每 {SUBMIT_COOLDOWN_SECONDS // 60} 分鐘可投稿一次"
            ),
            color=_COLOR_PANEL,
        )
        embed.set_footer(text="需要審核時，審核結果會以私訊通知")
        await channel.send(embed=embed, view=AnonymousPanelView())

    # ------------------------------------------------------------------
    # Slash commands (/anon …)
    # ------------------------------------------------------------------

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await _reply(interaction, _ADMIN_ONLY)
            return
        # With a cog-level handler in place the command tree no longer logs.
        self.logger.error("Anonymous board command failed", exc_info=error)
        await _reply(interaction, _GENERIC_ERROR)

    @app_commands.command(name="setup", description="設定匿名版的匿名頻道、後台頻道與是否需要審核")
    @app_commands.describe(
        public_channel="公開發布匿名投稿的文字頻道",
        review_channel="管理員審核與紀錄用的後台頻道（會顯示投稿者）",
        require_review="是否需要管理員審核後才發布，預設為是",
    )
    @admin_only()
    async def anon_setup(
        self,
        interaction: discord.Interaction,
        public_channel: discord.TextChannel,
        review_channel: discord.TextChannel,
        require_review: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            seeded = await self.configure(public_channel, review_channel, require_review=require_review)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        lines = [
            "✅ 匿名版設定完成",
            f"匿名頻道：{public_channel.mention}",
            f"後台頻道：{review_channel.mention}",
            f"審核模式：{'需審核後才發布' if require_review else '投稿立即發布'}",
        ]
        if seeded:
            lines.append(f"已建立預設分類：{'、'.join(DEFAULT_CATEGORIES)}")
        lines.append("接著在要放投稿按鈕的頻道執行 `/anon send_panel`。")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="status", description="查看匿名版目前的設定與待審核數量")
    @admin_only()
    async def anon_status(self, interaction: discord.Interaction) -> None:
        config = await self.get_config()
        categories = await self.database.list_anonymous_categories()
        embed = discord.Embed(title="匿名版狀態", color=_COLOR_PANEL)
        if config is None:
            embed.description = "尚未設定，請先執行 `/anon setup`。"
        else:
            embed.add_field(name="匿名頻道", value=f"<#{config.public_channel_id}>", inline=True)
            embed.add_field(name="後台頻道", value=f"<#{config.review_channel_id}>", inline=True)
            embed.add_field(
                name="審核模式",
                value="需審核後才發布" if config.require_review else "投稿立即發布",
                inline=True,
            )
            embed.add_field(
                name="發布通知",
                value=f"<@&{config.notify_role_id}>" if config.notify_role_id else "未設定",
                inline=True,
            )
        embed.add_field(
            name=f"分類（{len(categories)}/{MAX_CATEGORIES}）",
            value="、".join(category.name for category in categories) or "（無）",
            inline=False,
        )
        embed.add_field(
            name="待審核",
            value=str(await self.database.count_anonymous_posts(PostStatus.PENDING)),
            inline=True,
        )
        embed.add_field(
            name="已發布",
            value=str(await self.database.count_anonymous_posts(PostStatus.PUBLISHED)),
            inline=True,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="notify", description="設定匿名版發布時要提及的身分組，不填則關閉通知")
    @app_commands.describe(role="每則匿名貼文發布時要提及的身分組，不填則關閉")
    @admin_only()
    async def anon_notify(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self.set_notify_role(role)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        if role is None:
            message = "✅ 已關閉匿名版發布通知。"
        else:
            message = f"✅ 之後每則匿名貼文發布時都會提及 {role.mention}。"
        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="send_panel", description="將匿名投稿面板發送到目前頻道")
    @admin_only()
    async def anon_send_panel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ 無法在此頻道發佈", ephemeral=True)
            return
        await self.post_panel(channel)
        message = "✅ 已將匿名投稿面板發送到頻道"
        if await self.get_config() is None:
            message += "\n⚠️ 匿名版尚未設定，請先執行 `/anon setup`，否則按鈕無法使用。"
        await interaction.followup.send(message, ephemeral=True)

    @category.command(name="add", description="新增匿名版分類")
    @app_commands.describe(name="分類名稱，可包含 emoji，例如「😡 我要靠北」")
    @admin_only()
    async def category_add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_CATEGORY_NAME_LENGTH],
    ) -> None:
        name = collapse_whitespace(name)
        if not name:
            await interaction.response.send_message("❌ 分類名稱不能是空白。", ephemeral=True)
            return
        if await self.database.count_anonymous_categories() >= MAX_CATEGORIES:
            await interaction.response.send_message(
                f"❌ 分類最多 {MAX_CATEGORIES} 個（下拉選單的上限）。",
                ephemeral=True,
            )
            return
        if not await self.database.add_anonymous_category(name):
            await interaction.response.send_message(f"❌ 分類「{name}」已經存在。", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ 已新增分類「{name}」。", ephemeral=True)

    @category.command(name="remove", description="刪除匿名版分類")
    @app_commands.describe(name="要刪除的分類名稱")
    @admin_only()
    async def category_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not await self.database.remove_anonymous_category(name):
            await interaction.response.send_message(f"❌ 找不到分類「{name}」。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ 已刪除分類「{name}」，已發布的投稿不受影響。",
            ephemeral=True,
        )

    @category_remove.autocomplete("name")
    async def category_remove_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        categories = await self.database.list_anonymous_categories()
        return [
            app_commands.Choice(name=category.name, value=category.name)
            for category in categories
            if current in category.name
        ][:25]

    @category.command(name="list", description="列出匿名版分類")
    @admin_only()
    async def category_list(self, interaction: discord.Interaction) -> None:
        categories = await self.database.list_anonymous_categories()
        if categories:
            body = "\n".join(
                f"{index}. {category.name}" for index, category in enumerate(categories, start=1)
            )
        else:
            body = "目前沒有分類，請用 `/anon category add` 新增。"
        await interaction.response.send_message(
            f"**匿名版分類（{len(categories)}/{MAX_CATEGORIES}）**\n{body}",
            ephemeral=True,
        )
