from __future__ import annotations

import logging
import secrets
import time

import discord
from discord.ext import commands

from school_discord_bot.db.database import Database
from school_discord_bot.services.email_service import EmailService

logger = logging.getLogger(__name__)

_COLOR_VERIFICATION = 0x5865F2  # Discord blurple
_COLOR_SUCCESS = 0x43B581       # green
_COLOR_ERROR = 0xED4245         # red

_EMAIL_DOMAIN = "@student.shch.tc.edu.tw"
_CODE_EXPIRY_SECONDS = 900  # 15 minutes
_EMAIL_PORTAL_URL = "https://erp.dali.tc.edu.tw/student"


class StudentIdModal(discord.ui.Modal, title="輸入學號"):
    """Modal for collecting the student ID during verification step 1."""

    student_id = discord.ui.TextInput(
        label="學號",
        placeholder="例如：312345",
        min_length=4,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("VerificationCog")
        if not isinstance(cog, VerificationCog):
            await interaction.response.send_message(
                "❌ 驗證模組尚未載入",
                ephemeral=True,
            )
            return

        await cog.handle_student_id_submit(interaction, self.student_id.value)


class VerificationCodeModal(discord.ui.Modal, title="輸入驗證碼"):
    """Modal for collecting the 6-digit verification code during step 2."""

    code = discord.ui.TextInput(
        label="驗證碼",
        placeholder="6 位數字",
        min_length=6,
        max_length=6,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("VerificationCog")
        if not isinstance(cog, VerificationCog):
            await interaction.response.send_message(
                "❌ 驗證模組尚未載入",
                ephemeral=True,
            )
            return

        await cog.handle_code_submit(interaction, self.code.value)


class EnterIdButton(discord.ui.Button):
    """Button 1: Opens the student ID modal."""

    def __init__(self) -> None:
        super().__init__(
            label="輸入學號",
            custom_id="verification:enter_id",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("VerificationCog")
        if not isinstance(cog, VerificationCog):
            await interaction.response.send_message(
                "❌ 驗證模組尚未載入",
                ephemeral=True,
            )
            return

        # Check if already verified
        if await cog.database.is_student_verified(interaction.user.id):
            await interaction.response.send_message(
                "✅ 你已經完成驗證了！",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(StudentIdModal())


class EnterCodeButton(discord.ui.Button):
    """Button 2: Opens the verification code modal."""

    def __init__(self) -> None:
        super().__init__(
            label="輸入驗證碼",
            custom_id="verification:enter_code",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("VerificationCog")
        if not isinstance(cog, VerificationCog):
            await interaction.response.send_message(
                "❌ 驗證模組尚未載入",
                ephemeral=True,
            )
            return

        # Check if already verified
        if await cog.database.is_student_verified(interaction.user.id):
            await interaction.response.send_message(
                "✅ 你已經完成驗證了！",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(VerificationCodeModal())


class VerificationPanelView(discord.ui.View):
    """Persistent view with 3 buttons: enter student ID, enter code, email portal link."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(EnterIdButton())
        self.add_item(EnterCodeButton())
        self.add_item(
            discord.ui.Button(
                label="電子郵件系統",
                url=_EMAIL_PORTAL_URL,
            )
        )


class VerificationCog(commands.Cog, name="VerificationCog"):
    """Cog for student verification via school email."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        database: Database,
        email_service: EmailService,
        verified_student_role_id: int,
        guild_id: int,
    ) -> None:
        self.bot = bot
        self.database = database
        self.email_service = email_service
        self.verified_student_role_id = verified_student_role_id
        self.guild_id = guild_id
        self.logger = logging.getLogger(f"{__name__}.VerificationCog")

    async def handle_student_id_submit(
        self,
        interaction: discord.Interaction,
        student_id: str,
    ) -> None:
        """Handle student ID submission: generate code, send email, save to DB."""
        await interaction.response.defer(ephemeral=True)

        # Check if already verified
        if await self.database.is_student_verified(interaction.user.id):
            await interaction.followup.send(
                "✅ 你已經完成驗證了！",
                ephemeral=True,
            )
            return

        # Rate limiting: check if user sent a request too recently
        pending = await self.database.get_pending_verification(interaction.user.id)
        if pending:
            last_sent = pending.get("last_sent_at", 0)
            time_since_last = time.time() - last_sent
            cooldown = 60  # 60 seconds cooldown
            if time_since_last < cooldown:
                remaining = int(cooldown - time_since_last)
                await interaction.followup.send(
                    f"⏰ 請稍後再試，你需要等待 {remaining} 秒才能重新發送驗證碼。",
                    ephemeral=True,
                )
                return

        # Generate 6-digit code (always 6 digits with leading zeros)
        code = f"{secrets.randbelow(1000000):06d}"
        expires_at = time.time() + _CODE_EXPIRY_SECONDS
        last_sent_at = time.time()

        # Save pending verification
        await self.database.upsert_pending_verification(
            user_id=interaction.user.id,
            student_id=student_id,
            code=code,
            expires_at=expires_at,
            last_sent_at=last_sent_at,
        )

        # Send email
        email_address = f"{student_id}{_EMAIL_DOMAIN}"
        try:
            await self.email_service.send_verification_code(
                to_address=email_address,
                student_id=student_id,
                code=code,
            )
        except Exception:
            self.logger.exception(
                "Failed to send verification email to %s",
                email_address,
            )
            await interaction.followup.send(
                "❌ 郵件發送失敗，請稍後再試或聯絡管理員。",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ 已寄送驗證碼到 `{email_address}`\n"
            f"請在 15 分鐘內點擊「輸入驗證碼」按鈕完成驗證。\n\n"
            f"💡 如果沒有收到郵件，請檢查垃圾郵件/垃圾信箱資料夾。",
            ephemeral=True,
        )

    async def handle_code_submit(
        self,
        interaction: discord.Interaction,
        code: str,
    ) -> None:
        """Handle verification code submission: validate, assign role, complete verification."""
        await interaction.response.defer(ephemeral=True)

        # Check if already verified
        if await self.database.is_student_verified(interaction.user.id):
            await interaction.followup.send(
                "✅ 你已經完成驗證了！",
                ephemeral=True,
            )
            return

        # Get pending verification
        pending = await self.database.get_pending_verification(interaction.user.id)
        if pending is None:
            await interaction.followup.send(
                "❌ 尚未申請驗證，請先點擊「輸入學號」按鈕。",
                ephemeral=True,
            )
            return

        # Check expiry
        now = time.time()
        if now > pending["expires_at"]:
            await self.database.delete_pending_verification(interaction.user.id)
            await interaction.followup.send(
                "❌ 驗證碼已過期，請重新申請驗證。",
                ephemeral=True,
            )
            return

        # Check code match (tolerate stray whitespace from copy-paste)
        submitted_code = code.strip()
        stored_code = str(pending["code"]).strip()

        if submitted_code != stored_code:
            await interaction.followup.send(
                "❌ 驗證碼錯誤，請重新輸入。\n"
                "💡 提示：驗證碼為 6 位數字，請確認沒有輸入錯誤。",
                ephemeral=True,
            )
            return

        # Assign role
        guild = interaction.guild or self.bot.get_guild(self.guild_id)
        if guild is None:
            self.logger.error("Guild %s not found", self.guild_id)
            await interaction.followup.send(
                "❌ 無法找到伺服器，請聯絡管理員。",
                ephemeral=True,
            )
            return

        # In a guild interaction Discord sends the full member object, so
        # interaction.user is already a Member. Fall back to the cache, then to
        # an API fetch — get_member alone returns None without the privileged
        # members intent, which the bot does not request.
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                self.logger.exception(
                    "Failed to fetch member %s in guild %s",
                    interaction.user.id,
                    guild.id,
                )
                member = None
        if member is None:
            await interaction.followup.send(
                "❌ 無法找到你的成員資料，請聯絡管理員。",
                ephemeral=True,
            )
            return

        role = guild.get_role(self.verified_student_role_id)
        if role is None:
            self.logger.error(
                "Role %s not found in guild %s",
                self.verified_student_role_id,
                self.guild_id,
            )
            await interaction.followup.send(
                "❌ 驗證身份組不存在，請聯絡管理員。",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(role)
        except Exception:
            self.logger.exception(
                "Failed to assign role %s to member %s",
                self.verified_student_role_id,
                interaction.user.id,
            )
            await interaction.followup.send(
                "❌ 指派身份組失敗，請聯絡管理員。",
                ephemeral=True,
            )
            return

        # Complete verification
        await self.database.delete_pending_verification(interaction.user.id)
        await self.database.insert_verified_student(
            user_id=interaction.user.id,
            student_id=pending["student_id"],
            verified_at=now,
        )

        await interaction.followup.send(
            f"✅ 驗證成功！已獲得 {role.mention} 身份組。",
            ephemeral=True,
        )

    async def post_panel(self, channel: discord.TextChannel) -> None:
        """Post the verification panel embed + view to a channel."""
        embed = discord.Embed(
            title="🎓 在校學生身份驗證",
            description=(
                "透過學校電子郵件驗證你的在校學生身份。\n\n"
                "**驗證步驟：**\n"
                "1. 點擊「**輸入學號**」按鈕，填寫你的學號\n"
                "2. 查收電子郵件（學號@student.shch.tc.edu.tw）\n"
                "3. 點擊「**輸入驗證碼**」按鈕，填入收到的 6 位數驗證碼\n"
                "4. 驗證成功後，你將自動獲得學生身份組\n\n"
                "驗證碼有效期限為 **15 分鐘**。"
            ),
            color=_COLOR_VERIFICATION,
        )
        embed.set_footer(text="如有問題請聯絡管理員")

        await channel.send(embed=embed, view=VerificationPanelView())
