"""Email configuration and delivery tools."""

import asyncio
import mimetypes
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from meteora.core.config import MeteoraConfig, save_email_smtp_password
from meteora.toolbox.paths import find_project_dir
from meteora.toolbox.registry import register_tool


def find_config() -> MeteoraConfig:
    project_dir = find_project_dir()
    config_path = project_dir / "meteora.yaml"
    if config_path.exists():
        return MeteoraConfig.load(config_path)
    return MeteoraConfig.create_default()


def find_config_path() -> Path:
    return find_project_dir() / "meteora.yaml"


@register_tool(
    name="configure_email_config",
    description=(
        "保存用户提供的邮箱 SMTP 配置。支持 QQ 邮箱、163 邮箱、Gmail 等通用 SMTP 服务。"
        "注意：QQ 邮箱和 163 邮箱需要使用授权码而非登录密码。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "smtp_host": {
                "type": "string",
                "description": "SMTP 服务器地址，如 smtp.qq.com、smtp.gmail.com",
            },
            "smtp_port": {"type": "integer", "description": "SMTP 端口，默认为 587"},
            "smtp_user": {"type": "string", "description": "邮箱账号（完整邮箱地址）"},
            "smtp_password": {
                "type": "string",
                "description": "SMTP 密码或授权码（QQ/163 邮箱需使用授权码）",
            },
            "from_name": {"type": "string", "description": "发件人显示名称，默认为 Meteora"},
        },
        "required": ["smtp_host", "smtp_user", "smtp_password"],
    },
)
def configure_email_config(
    smtp_host: str,
    smtp_user: str,
    smtp_password: str,
    smtp_port: int = 587,
    from_name: str = "",
) -> dict:
    config = find_config()
    config.email.enabled = True
    config.email.smtp_host = smtp_host
    config.email.smtp_port = smtp_port
    config.email.smtp_user = smtp_user
    if from_name:
        config.email.from_name = from_name
    config.save(find_config_path())
    save_email_smtp_password(smtp_password)
    return {
        "status": "success",
        "message": (
            f"邮箱配置已保存。发件人: {config.email.from_name} <{smtp_user}>，"
            f"SMTP: {smtp_host}:{smtp_port}"
        ),
    }


@register_tool(
    name="check_email_config",
    description=(
        "检查邮箱 SMTP 配置是否已完成。用户询问「邮箱配置好了吗」、"
        "发邮件、邮件通知等功能是否能使用时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)
def check_email_config() -> dict:
    config = find_config()
    email_cfg = config.email
    configured = bool(email_cfg.enabled and email_cfg.smtp_user and email_cfg.smtp_password)
    if configured:
        default_to_info = (
            f"默认收件人: {email_cfg.default_to}"
            if email_cfg.default_to.strip()
            else "无默认收件人（将发给自己）"
        )
        message = (
            f"邮箱已配置。发件人: {email_cfg.from_name} <{email_cfg.smtp_user}>，"
            f"SMTP: {email_cfg.smtp_host}:{email_cfg.smtp_port}，{default_to_info}。"
        )
    else:
        missing = []
        if not email_cfg.enabled or not email_cfg.smtp_user:
            missing.append("SMTP 配置未完成（缺少 smtp_host 或 smtp_user）")
        if not email_cfg.smtp_password:
            missing.append("SMTP 密码/授权码未设置")
        reason = "；".join(missing) if missing else "邮箱未配置"
        message = (
            f"邮箱尚未配置。（{reason}）\n\n"
            "常用 SMTP 参数：\n"
            "- QQ邮箱: smtp.qq.com, 端口 587, 需要授权码\n"
            "- 163邮箱: smtp.163.com, 端口 25 或 465\n"
            "- Gmail: smtp.gmail.com, 端口 587\n\n"
            "请告诉我你的 SMTP 服务器地址、邮箱账号和密码/授权码，我会帮你配置。"
        )
    return {
        "status": "configured" if configured else "not_configured",
        "configured": configured,
        "message": message,
    }


@register_tool(
    name="send_email",
    description=(
        "发送邮件。当用户要求发送邮件时直接调用，不要先检查配置。"
        "如果邮箱未配置，本工具会返回配置引导。支持纯文本、HTML 和附件。"
        "除非用户明确要求，否则不得擅自发送邮件。"
        "未指定收件人时使用默认收件人或发件人自己。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    '收件人邮箱地址列表（可选），如 ["zhang@example.com"]。'
                    "不填则使用默认收件人或发件人自己"
                ),
            },
            "subject": {"type": "string", "description": "邮件主题"},
            "body": {
                "type": "string",
                "description": "邮件正文，支持纯文本或 HTML（以 < 开头视为 HTML）",
            },
            "cc": {
                "type": "array",
                "items": {"type": "string"},
                "description": "抄送邮箱地址列表（可选）",
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": '附件文件路径列表（可选），如 ["figures/precip.png"]',
            },
            "from_name": {
                "type": "string",
                "description": "本次发送的发件人名称（可选），不填则使用已配置的默认名称",
            },
        },
        "required": ["subject", "body"],
    },
)
async def send_email(
    subject: str,
    body: str,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    attachments: list[str] | None = None,
    from_name: str = "",
) -> dict:
    config = find_config()
    email_cfg = config.email
    if not email_cfg.enabled or not email_cfg.smtp_user:
        return {
            "status": "error",
            "message": (
                "邮箱尚未配置。请先提供 SMTP 配置信息。\n\n"
                "常用 SMTP 参数：\n"
                "- QQ邮箱: smtp.qq.com, 端口 587, 需要授权码\n"
                "- 163邮箱: smtp.163.com, 端口 25 或 465\n"
                "- Gmail: smtp.gmail.com, 端口 587\n\n"
                "请告诉我你的 SMTP 服务器地址、邮箱账号和密码/授权码，我会帮你配置。"
            ),
        }

    recipients = to or []
    if not recipients:
        default_to = email_cfg.default_to.strip()
        if default_to:
            recipients = [default_to]
        else:
            recipients = [email_cfg.smtp_user]

    display_name = from_name or email_cfg.from_name or "Meteora"
    msg = MIMEMultipart()
    msg["From"] = f"{display_name} <{email_cfg.smtp_user}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    all_recipients = list(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
        all_recipients.extend(cc)

    subtype = "html" if body.strip().startswith("<") else "plain"
    msg.attach(MIMEText(body, subtype, "utf-8"))

    attached_files: list[str] = []
    for file_path in attachments or []:
        path = Path(file_path)
        if not path.exists():
            return {"status": "error", "message": f"附件文件不存在: {file_path}"}
        if not path.is_file():
            return {"status": "error", "message": f"附件路径不是文件: {file_path}"}
        file_size = path.stat().st_size
        max_size = 10 * 1024 * 1024
        if file_size > max_size:
            return {
                "status": "error",
                "message": f"附件 {path.name} 大小 {file_size / 1024 / 1024:.1f}MB 超过 10MB 限制",
            }
        mime_type, _ = mimetypes.guess_type(str(path))
        main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{path.name}"')
        msg.attach(part)
        attached_files.append(path.name)

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _send_email_sync(
                email_cfg.smtp_host,
                email_cfg.smtp_port,
                email_cfg.smtp_tls,
                email_cfg.smtp_user,
                email_cfg.smtp_password,
                msg,
                all_recipients,
            ),
        )
        parts: list[str] = [f"邮件已发送至 {', '.join(recipients)}"]
        if cc:
            parts.append(f"抄送: {', '.join(cc)}")
        if attached_files:
            parts.append(f"附件: {', '.join(attached_files)}")
        return {"status": "success", "message": "；".join(parts)}
    except smtplib.SMTPAuthenticationError:
        return {"status": "error", "message": "SMTP 认证失败，请检查邮箱账号和密码/授权码是否正确"}
    except smtplib.SMTPException as e:
        return {"status": "error", "message": f"邮件发送失败: {e}"}


def _send_email_sync(
    host: str,
    port: int,
    tls: bool,
    user: str,
    password: str,
    msg: MIMEMultipart,
    recipients: list[str],
) -> None:
    with smtplib.SMTP(host, port, timeout=30) as server:
        if tls:
            server.starttls()
        server.login(user, password)
        server.send_message(msg, to_addrs=recipients)
