"""
Email delivery for the CC Tools platform via Gmail SMTP.

All emails contain only user-facing display information.
No API keys, secrets, or internal paths are ever included in email bodies.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional


def _send(to: str, subject: str, html_body: str) -> bool:
    """Send an email via Gmail SMTP. Returns True on success."""
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_password:
        print("[EMAIL] GMAIL_USER or GMAIL_APP_PASSWORD not configured - email skipped")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, gmail_password)
            smtp.sendmail(gmail_user, to, msg.as_string())
        return True
    except Exception as exc:
        print(f"[EMAIL] Gmail SMTP error sending to {to}: {exc}")
        return False


def _base_html(title: str, body: str) -> str:
    """Minimal email wrapper with consistent styling."""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:32px;">
  <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:8px;
              padding:32px;border:1px solid #e0e0e0;">
    <div style="margin-bottom:24px;">
      <span style="font-size:13px;font-weight:600;color:#c0392b;letter-spacing:1px;">
        Enterprise Client
      </span>
      <span style="font-size:13px;color:#888;margin-left:8px;">Tools Platform</span>
    </div>
    {body}
    <hr style="margin:32px 0;border:none;border-top:1px solid #eee;">
    <p style="font-size:11px;color:#aaa;margin:0;">
      This is an automated message from the CC Tools platform.
      Do not reply to this email.
    </p>
  </div>
</body>
</html>"""


def send_admin_notification(
    admin_email: str,
    user_name: str,
    user_email: str,
    app_url: str,
) -> bool:
    """
    Notify the admin that a new user has requested access.
    Email contains no tokens or secrets - admin must log in normally to approve.
    """
    body = f"""
    <h2 style="margin:0 0 16px;color:#222;font-size:20px;">New Access Request</h2>
    <p style="color:#555;line-height:1.6;">
      <strong>{user_name}</strong> ({user_email}) has requested access to the CC Tools platform.
    </p>
    <p style="color:#555;line-height:1.6;">
      Log in to review and approve this request:
    </p>
    <a href="{app_url}/login" style="display:inline-block;margin:8px 0 24px;padding:12px 24px;
       background:#c0392b;color:#fff;text-decoration:none;border-radius:4px;font-weight:600;">
      Log In to Approve
    </a>
    <p style="font-size:12px;color:#888;">
      Once logged in, navigate to the Admin tab to approve or deny this request.
    </p>
    """
    return _send(
        to=admin_email,
        subject=f"CC Tools: Access request from {user_name}",
        html_body=_base_html("Access Request", body),
    )


def send_temp_password(
    user_email: str,
    user_name: str,
    temp_password: str,
    app_url: str,
) -> bool:
    """
    Send an approved user their temporary password.
    They must change it on first login - this password is one-time use.
    """
    body = f"""
    <h2 style="margin:0 0 16px;color:#222;font-size:20px;">Your Access Is Ready</h2>
    <p style="color:#555;line-height:1.6;">
      Hi {user_name}, your request to access the CC Tools platform has been approved.
    </p>
    <p style="color:#555;line-height:1.6;">
      Use the temporary password below to log in. You will be asked to set your own
      password immediately after your first login.
    </p>
    <div style="margin:24px 0;padding:16px 24px;background:#f8f8f8;border-radius:4px;
                border-left:4px solid #c0392b;">
      <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#888;
                letter-spacing:1px;text-transform:uppercase;">Temporary Password</p>
      <p style="margin:0;font-family:monospace;font-size:22px;color:#222;
                letter-spacing:2px;">{temp_password}</p>
    </div>
    <a href="{app_url}/login" style="display:inline-block;margin:8px 0 24px;padding:12px 24px;
       background:#c0392b;color:#fff;text-decoration:none;border-radius:4px;font-weight:600;">
      Log In Now
    </a>
    <p style="font-size:12px;color:#888;">
      This password is for one-time use. You will set your permanent password on first login.
      If you did not request this access, please contact your administrator.
    </p>
    """
    return _send(
        to=user_email,
        subject="CC Tools: Your access credentials",
        html_body=_base_html("Access Credentials", body),
    )


def send_feedback_notification(
    admin_email: str,
    user_name: str,
    user_email: str,
    tool_label: str,
    feedback_text: str,
    app_url: str,
) -> bool:
    """
    Notify the admin that a user submitted feedback after a tool run.
    """
    body = f"""
    <h2 style="margin:0 0 16px;color:#222;font-size:20px;">Tool Feedback Received</h2>
    <p style="color:#555;line-height:1.6;">
      <strong>{user_name}</strong> ({user_email}) left feedback after using
      <strong>{tool_label}</strong>.
    </p>
    <div style="margin:24px 0;padding:16px 24px;background:#f8f8f8;border-radius:4px;
                border-left:4px solid #c0392b;">
      <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#888;
                letter-spacing:1px;text-transform:uppercase;">Feedback</p>
      <p style="margin:0;font-size:15px;color:#222;line-height:1.6;">{feedback_text}</p>
    </div>
    <p style="font-size:12px;color:#888;">
      This has been logged to the CC Tools Log sheet. You can review all feedback there.
    </p>
    """
    return _send(
        to=admin_email,
        subject=f"CC Tools: Feedback from {user_name} on {tool_label}",
        html_body=_base_html("Tool Feedback", body),
    )


def send_reset_link(
    user_email: str,
    reset_url: str,
) -> bool:
    """
    Send a password reset link. The URL contains a signed token - not a raw secret.
    Link expires in 1 hour.
    """
    body = f"""
    <h2 style="margin:0 0 16px;color:#222;font-size:20px;">Reset Your Password</h2>
    <p style="color:#555;line-height:1.6;">
      We received a request to reset the password for your CC Tools account.
      Click the button below to set a new password.
    </p>
    <a href="{reset_url}" style="display:inline-block;margin:8px 0 24px;padding:12px 24px;
       background:#c0392b;color:#fff;text-decoration:none;border-radius:4px;font-weight:600;">
      Reset My Password
    </a>
    <p style="font-size:12px;color:#888;">
      This link expires in 1 hour. If you did not request a password reset,
      you can safely ignore this email - your password has not been changed.
    </p>
    """
    return _send(
        to=user_email,
        subject="CC Tools: Password reset request",
        html_body=_base_html("Password Reset", body),
    )

# rev 3
