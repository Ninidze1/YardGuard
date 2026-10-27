"""Alerts and the daily summary by email over SMTP (Gmail, Outlook, Yahoo or any mail server).

(Named emailer.py, not email.py: that would hide Python's own email package.)

One mail per message: a plain-text part (the same lines every channel uses) and an HTML part with
the close-up shown inline; with pictures "both" the whole picture is attached as well. Mails never
change after sending, so later news about an incident (escalation, a status change) is a reply
("Re: ...") in the same conversation. The password is stored encrypted and scrubbed from every
error; SMTP errors are turned into plain words that point at the setup field to fix.
"""
from __future__ import annotations

import html
import logging
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import media
from .base import Channel, ChannelError
from .base import scrub as _scrub
from .model import CheckResult, Event, SendContext, SendResult

log = logging.getLogger("yardguard")

PRESETS = {"gmail": {"host": "smtp.gmail.com", "port": 587, "security": "starttls",
                     "help": "Gmail needs an app password, not your normal password. Turn on 2-Step "
                             "Verification first, then create an app password.",
                     "help_url": "https://myaccount.google.com/apppasswords"},
           "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "security": "starttls",
                       "help": "Outlook.com may refuse passwords from apps; if it does, use Gmail or your "
                               "provider's SMTP."},
           "yahoo": {"host": "smtp.mail.yahoo.com", "port": 465, "security": "ssl",
                     "help": "Yahoo needs an app password (Account security > Generate app password).",
                     "help_url": "https://login.yahoo.com/account/security"},
           "other": {"host": "", "port": 587, "security": "starttls"}}
PRESET_TITLES = {"gmail": "Gmail", "outlook": "Outlook / Hotmail", "yahoo": "Yahoo", "other": "Other"}
SECURITY = ("starttls", "ssl", "none")
TIMEOUT_S = 15
MAX_UPLOAD = 10_000_000
PICTURE_BYTES = 3_000_000
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
SEVERITY_WORD = {"critical": "Critical", "warning": "Warning", "info": "Info"}
SEVERITY_COLOR = {"critical": "#dc2626", "warning": "#d97706", "info": "#2563eb"}
INCIDENT_KINDS = ("incident_new", "incident_update", "incident_status")
WRONG_PASSWORD = "Password not accepted. Gmail and Yahoo need an app password."
PORT_465 = "Port 465 needs the SSL setting (or use port 587 with STARTTLS)."


def scrub(text: str, *secrets: str) -> str:
    """base.scrub without the empty strings."""
    return _scrub(text, *[s for s in secrets if s])


def parse_recipients(text) -> list[str]:
    """"a@x.com; b@y.org, " -> ["a@x.com", "b@y.org"] (also accepts a list)."""
    items = text if isinstance(text, (list, tuple)) else re.split(r"[,;\s]+", str(text or ""))
    out: list[str] = []
    for item in items:
        item = str(item).strip().strip("<>")
        if item and item.lower() not in (o.lower() for o in out):
            out.append(item)
    return out


def valid_address(addr: str) -> bool:
    return bool(EMAIL_RE.match(addr or ""))


def apply_preset(params: dict, preset: str) -> dict:
    """Sets preset, host, port and security of params from PRESETS ("other" keeps a typed host)."""
    p = PRESETS.get(preset, PRESETS["other"])
    params["preset"] = preset if preset in PRESETS else "other"
    if preset != "other" or not params.get("host"):
        params["host"] = p["host"]
    params["port"] = p["port"]
    params["security"] = p["security"]
    return params


def guess_preset(address: str) -> str:
    """The provider of an address: gmail / outlook / yahoo / other."""
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in ("gmail.com", "googlemail.com"):
        return "gmail"
    if domain in ("outlook.com", "hotmail.com", "live.com", "msn.com") or domain.startswith(("outlook.", "hotmail.")):
        return "outlook"
    if domain.startswith("yahoo.") or domain in ("ymail.com", "rocketmail.com"):
        return "yahoo"
    return "other"


def _settings(params: dict) -> tuple[str, int, str]:
    preset = PRESETS.get(params.get("preset", ""), {})
    host = str(params.get("host") or preset.get("host") or "").strip()
    try:
        port = int(params.get("port") or preset.get("port") or 587)
    except (TypeError, ValueError):
        port = 0
    security = params.get("security") or preset.get("security") or "starttls"
    return host, port, security if security in SECURITY else "starttls"


def _ehlo_name() -> str:
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    return name if name and name.isascii() else "localhost"


def smtp_error(exc: BaseException, host: str, port: int, security: str = "") -> ChannelError:
    """Plain words for an SMTP / network failure, pointing at the setup field to fix."""
    if isinstance(exc, ChannelError):
        return exc
    where = f"{host}:{port}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ChannelError(WRONG_PASSWORD, permanent=True, field="password")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        bad = ", ".join(list(exc.recipients)[:3])
        return ChannelError(f"The mail server refused the address {bad}.", permanent=True, field="to")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return ChannelError(f"The mail server refused the sender address {exc.sender}. Send from your own "
                            "address.", permanent=True, field="sender")
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return ChannelError("This mail server does not accept a login on this port. Check the port and the "
                            "security setting.", permanent=True, field="security")
    wrong_port = port == 465 and security != "ssl"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        if wrong_port:
            return ChannelError(PORT_465, permanent=True, field="security")
        return ChannelError(f"The mail server {host} closed the connection.", retry=True)
    if isinstance(exc, smtplib.SMTPConnectError):
        return ChannelError(f"Could not reach {where}. Check the internet connection or the port.", retry=True,
                            field="port")
    if isinstance(exc, smtplib.SMTPResponseException):
        code = exc.smtp_code
        msg = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        msg = " ".join(msg.split())[:150]
        if code in (534, 535):
            return ChannelError(WRONG_PASSWORD, permanent=True, field="password")
        if 400 <= code < 500:
            return ChannelError(f"The mail server is busy ({code}). Trying again later.", retry=True)
        return ChannelError(f"The mail server refused the message ({code}): {msg}", permanent=True)
    if isinstance(exc, smtplib.SMTPException):
        return ChannelError(f"Mail problem: {exc}", retry=True)
    if isinstance(exc, socket.gaierror):
        return ChannelError(f"Could not find the mail server {host}.", retry=True, field="host")
    if isinstance(exc, ssl.SSLError):
        return ChannelError("Secure connection failed. Try port 587 with STARTTLS.", permanent=True,
                            field="security")
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError)):
        if wrong_port and isinstance(exc, (TimeoutError, socket.timeout)):
            return ChannelError(PORT_465, permanent=True, field="security")
        return ChannelError(f"Could not reach {where}. Check the internet connection or the port.", retry=True,
                            field="port")
    if isinstance(exc, OSError):
        return ChannelError(f"Could not reach {where}. Check the internet connection.", retry=True, field="host")
    return ChannelError(f"Mail problem: {exc}", retry=True)


def connect(params: dict, password: str, timeout: float = TIMEOUT_S) -> smtplib.SMTP:
    """An SMTP connection, secured as configured and logged in. Raises ChannelError."""
    host, port, security = _settings(params)
    username = str(params.get("username") or "").strip()
    if not host:
        raise ChannelError("Enter the mail server (for example smtp.gmail.com).", permanent=True, field="host")
    if not 0 < port < 65536:
        raise ChannelError("The port must be a number such as 587.", permanent=True, field="port")
    smtp = None
    try:
        context = ssl.create_default_context()
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, local_hostname=_ehlo_name(), timeout=timeout, context=context)
        else:
            smtp = smtplib.SMTP(host, port, local_hostname=_ehlo_name(), timeout=timeout)
            smtp.ehlo()
            if security == "starttls":
                if not smtp.has_extn("starttls"):
                    raise ChannelError(f"{host} does not offer a secure connection on port {port}. Check the "
                                       "port and the security setting.", permanent=True, field="security")
                smtp.starttls(context=context)
                smtp.ehlo()
        if username:
            if not password:
                raise ChannelError("Enter the app password.", permanent=True, field="password")
            smtp.login(username, password)
        return smtp
    except BaseException as e:
        if smtp is not None:
            try:
                smtp.close()
            except Exception:
                pass
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        raise smtp_error(e, host, port, security) from None


# --- Message building -------------------------------------------------------------

def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def subject_for(event: Event, prefix: str = "YardGuard") -> str:
    """"[YardGuard] Critical: Person near the car at night - Front gate 02:14"."""
    tag = f"[{_one_line(prefix) or 'YardGuard'}] "
    head = _one_line(media.headline(event) or event.title or "YardGuard")
    cam = _one_line(event.camera)
    when = media.clock(event.ts) if event.ts else ""
    if event.kind in INCIDENT_KINDS or (event.kind in ("area", "plate") and event.severity != "info"):
        tail = " ".join(x for x in ((cam if cam and cam not in head else ""), when) if x)
        text = f"{SEVERITY_WORD.get(event.severity, 'Info')}: {head}" + (f" - {tail}" if tail else "")
    else:
        text = head
    return (tag + text)[:200]


def _summary_rows(event: Event) -> str:
    rows = []
    for line in [ln for ln in str(event.text or "").splitlines() if ln.strip()]:
        if ": " in line:
            k, v = line.split(": ", 1)
            rows.append(f"<tr><td style=\"padding:6px 12px 6px 0;color:#64748b\">{html.escape(k)}</td>"
                        f"<td style=\"padding:6px 0\">{html.escape(v)}</td></tr>")
        else:
            rows.append(f"<tr><td colspan=\"2\" style=\"padding:6px 0\">{html.escape(line)}</td></tr>")
    if not rows:
        return ""
    return ("<table style=\"border-collapse:collapse;font-size:14px;margin:8px 0 12px\">" + "".join(rows)
            + "</table>")


def build_message(event: Event, ctx: SendContext, params: dict) -> EmailMessage:
    """The mail for an event (headers, text, HTML with the inline picture, attachment)."""
    sender = str(params.get("sender") or params.get("username") or "").strip()
    to = parse_recipients(params.get("to") or [sender])
    prefix = str(params.get("subject_prefix") or "YardGuard")
    msg = EmailMessage()
    subject = subject_for(event, prefix)
    ref = ctx.thread_ref or {}
    if ref.get("message_id") and event.kind in ("incident_update", "incident_status"):
        first = _one_line(ref.get("subject") or subject)
        subject = first if first.lower().startswith("re:") else "Re: " + first
        msg["In-Reply-To"] = ref["message_id"]
        msg["References"] = ref["message_id"]
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "yardguard.local"
    msg["Message-ID"] = make_msgid("yardguard", domain=domain)
    if event.severity == "critical" and event.kind not in ("test", "summary"):
        msg["X-Priority"] = "1"
        msg["Importance"] = "High"

    lines = [ln for ln in media.lines(event) if ln]
    link = ""
    if ctx.web_base and event.web_path:
        link = ctx.web_base.rstrip("/") + "/" + event.web_path.lstrip("/")
    text = media.plain(event)
    if event.kind == "summary" and event.text and event.text not in text:
        text += "\n\n" + event.text
    if link:
        text += f"\n\nOpen in YardGuard: {link}"
    text += "\n\n-- \nSent by YardGuard on your PC. Change what is sent on the Integrations page."
    msg.set_content(text)

    pictures = ctx.pictures
    inline_path = attach_path = ""
    if pictures == "both":
        inline_path = event.image_path("crop")
        snap = event.image_path("snapshot")
        attach_path = snap if snap and snap != inline_path else ""
    elif pictures in ("crop", "snapshot"):
        inline_path = event.image_path(pictures)
    inline = media.prepare_jpeg(inline_path, 1280, 80, PICTURE_BYTES) if inline_path else None
    attach = media.prepare_jpeg(attach_path, 1920, 85, PICTURE_BYTES) if attach_path else None

    color = SEVERITY_COLOR.get(event.severity, SEVERITY_COLOR["info"])
    head = html.escape(lines[0] if lines else event.title or "YardGuard")
    body = "".join(f"<p style=\"margin:4px 0;color:#334155\">{html.escape(ln)}</p>" for ln in lines[1:])
    if event.kind == "summary":
        body += _summary_rows(event)
    cid = make_msgid("pic", domain="yardguard.local")
    img = (f"<p style=\"margin:12px 0\"><img src=\"cid:{cid[1:-1]}\" alt=\"Picture\" "
           f"style=\"max-width:100%;border-radius:8px\"></p>") if inline else ""
    button = (f"<p style=\"margin:16px 0\"><a href=\"{html.escape(link, quote=True)}\" style=\"background:{color};"
              f"color:#ffffff;padding:8px 14px;border-radius:6px;text-decoration:none\">Open in YardGuard</a></p>"
              if link.startswith("https://") else "")
    page = (f"<!doctype html><html><body style=\"font-family:Segoe UI,Arial,sans-serif;font-size:14px;"
            f"color:#0f172a;margin:0;padding:16px\"><div style=\"max-width:640px\">"
            f"<div style=\"border-left:4px solid {color};padding-left:12px\">"
            f"<h2 style=\"margin:0 0 6px;font-size:18px\">{head}</h2>{body}</div>{img}{button}"
            f"<p style=\"color:#94a3b8;font-size:12px;margin-top:20px\">Sent by YardGuard on your PC. Change what "
            f"is sent on the Integrations page.</p></div></body></html>")
    msg.add_alternative(page, subtype="html")
    if inline:
        msg.get_payload()[1].add_related(inline, maintype="image", subtype="jpeg", cid=cid,
                                         filename="closeup.jpg", disposition="inline")
    if attach:
        msg.add_attachment(attach, maintype="image", subtype="jpeg", filename="picture.jpg")
    return msg


# --- Channel --------------------------------------------------------------------

class EmailChannel(Channel):
    KIND = "email"
    CAPS = {"pictures": True, "clip": False, "edit": False, "buttons": False, "two_way": False,
            "max_upload": MAX_UPLOAD, "updates": "drop"}
    SECRET_KEYS = ("password",)

    @classmethod
    def validate(cls, params: dict, secrets: dict[str, str]) -> CheckResult | None:
        """Form checks without network; None when everything looks right."""
        host, port, _security = _settings(params)
        sender = str(params.get("sender") or params.get("username") or "").strip()
        to = parse_recipients(params.get("to") or [sender])
        if not host:
            return CheckResult(False, "Enter the mail server (for example smtp.gmail.com).", field="host")
        if not 0 < port < 65536:
            return CheckResult(False, "The port must be a number such as 587.", field="port")
        if not valid_address(sender):
            return CheckResult(False, "Enter your email address.", field="sender")
        if params.get("username") and not (secrets.get("password") or "").strip():
            return CheckResult(False, "Enter the app password.", field="password")
        bad = [a for a in to if not valid_address(a)]
        if not to or bad:
            return CheckResult(False, f"Check the address {bad[0]}." if bad else "Enter where to send the mails.",
                               field="to")
        return None

    @classmethod
    def check(cls, params: dict, secrets: dict[str, str]) -> CheckResult:
        """Connects and logs in, then quits. Sends no mail."""
        problem = cls.validate(params, secrets)
        if problem:
            return problem
        password = secrets.get("password") or ""
        host, port, _security = _settings(params)
        try:
            smtp = connect(params, password)
            try:
                smtp.quit()
            except Exception:
                pass
            who = str(params.get("username") or "").strip()
            return CheckResult(True, f"Logged in to {host} as {who}" if who else f"Connected to {host}",
                               data={"host": host, "port": port})
        except ChannelError as e:
            return CheckResult(False, scrub(str(e), password), field=getattr(e, "field", "") or "host")
        except Exception as e:
            return CheckResult(False, scrub(f"Could not check the mail server: {e}", password), field="host")

    def send(self, event: Event, ctx: SendContext) -> SendResult:
        started = time.monotonic()
        password = ""
        try:
            password = self.conf.secret("password")
            if ctx.edit:
                return SendResult(ok=True, note="Emails are not changed after sending")
            params = self.conf.params
            if params.get("username") and not password:
                raise ChannelError("The saved password cannot be read on this Windows account. Enter it again.",
                                   permanent=True, field="password")
            problem = self.validate(params, {"password": password})
            if problem:
                raise ChannelError(problem.message, permanent=True, field=problem.field)
            msg = build_message(event, ctx, params)
            to = parse_recipients(params.get("to") or [msg["From"]])
            if not to:
                raise ChannelError("No address to send to.", permanent=True, field="to")
            host, port, security = _settings(params)
            smtp = connect(params, password)
            try:
                refused = smtp.send_message(msg, from_addr=str(msg["From"]), to_addrs=to)
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                raise smtp_error(e, host, port, security) from None
            finally:
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass
            note = f"Not delivered to {', '.join(refused)}" if refused else ""
            ref = {"message_id": str(msg["Message-ID"]), "subject": str(msg["Subject"])}
            return SendResult(ok=True, ref=ref, ms=int((time.monotonic() - started) * 1000), note=note)
        except Exception as e:
            ms = int((time.monotonic() - started) * 1000)
            if not isinstance(e, ChannelError):
                log.warning("Email: unexpected error (%s)", type(e).__name__)
                e = ChannelError(f"Unexpected problem: {e}", retry=True)
            return SendResult(ok=False, error=scrub(str(e), password), retry=bool(getattr(e, "retry", False)),
                              retry_after=getattr(e, "retry_after", None),
                              permanent=bool(getattr(e, "permanent", False)), ms=ms)

    def describe(self) -> str:
        params = self.conf.params
        to = parse_recipients(params.get("to") or [params.get("sender", "")])
        provider = PRESET_TITLES.get(params.get("preset", ""), "")
        who = to[0] if to else "nobody yet"
        more = f" (+{len(to) - 1})" if len(to) > 1 else ""
        via = f" via {provider}" if provider and provider != "Other" else ""
        return f"To {who}{more}{via}"
