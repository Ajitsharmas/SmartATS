# ---------------------------------------------------------------------------
# Purpose: Transactional email sending via Resend
# ---------------------------------------------------------------------------

import html
import resend

from app.config import settings

resend.api_key = settings.RESEND_API_KEY

_WRAPPER = """<!DOCTYPE html>
<html>
<body style="font-family:Inter,sans-serif;background:#f8fafc;margin:0;padding:32px;">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;border:1px solid #e2e8f0;padding:40px;">
    <p style="font-size:22px;font-weight:700;color:#4f46e5;margin:0 0 24px;">SmartATS</p>
    {body}
    <hr style="border:none;border-top:1px solid #e2e8f0;margin:32px 0 16px;">
    <p style="color:#94a3b8;font-size:12px;margin:0;">
      If you did not request this, you can safely ignore this email.
    </p>
  </div>
</body>
</html>"""

_BUTTON = (
    '<a href="{url}" style="display:inline-block;margin-top:20px;background:#4f46e5;color:#fff;'
    'text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;">{label}</a>'
    '<p style="color:#94a3b8;font-size:11px;margin-top:12px;word-break:break-all;">Or copy: {url}</p>'
)


def _send(to: str, subject: str, body: str) -> None:
    resend.Emails.send({
        "from": settings.FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "html": _WRAPPER.format(body=body),
    })


def send_verification_email(to_email: str, full_name: str, token: str) -> None:
    name = html.escape(full_name or "there")
    url = f"{settings.APP_BASE_URL}/verify-email?token={token}"
    body = f"""
      <p style="color:#1e293b;font-size:16px;margin:0 0 12px;">Hi {name},</p>
      <p style="color:#475569;margin:0 0 8px;">
        Thanks for signing up for SmartATS. Please verify your email address to activate your account.
      </p>
      <p style="color:#f59e0b;font-size:13px;margin:0;">
        This link expires in <strong>10 minutes</strong>.
      </p>
      {_BUTTON.format(url=url, label="Verify Email Address")}
    """
    _send(to_email, "Verify your SmartATS email address", body)


def send_password_reset_email(to_email: str, full_name: str, token: str) -> None:
    name = html.escape(full_name or "there")
    url = f"{settings.APP_BASE_URL}/reset-password?token={token}"
    body = f"""
      <p style="color:#1e293b;font-size:16px;margin:0 0 12px;">Hi {name},</p>
      <p style="color:#475569;margin:0 0 8px;">
        We received a request to reset your SmartATS password.
        Click the button below to choose a new password.
      </p>
      <p style="color:#f59e0b;font-size:13px;margin:0;">
        This link expires in <strong>15 minutes</strong>.
      </p>
      {_BUTTON.format(url=url, label="Reset Password")}
    """
    _send(to_email, "Reset your SmartATS password", body)


def send_application_scored_email(
    to_email: str, candidate_name: str, job_title: str, is_rescore: bool = False
) -> None:
    name = html.escape(candidate_name or "there")
    title = html.escape(job_title)

    if is_rescore:
        subject = f"Update: your application for {job_title} has been re-reviewed"
        action_line = (
            "Following an update to the job posting, your application for the "
            f"<strong>{title}</strong> position has been <strong>re-reviewed and rescored</strong> by our system."
        )
    else:
        subject = f"Your application for {job_title} has been reviewed"
        action_line = (
            f"Thank you for applying for the <strong>{title}</strong> position. "
            "Your application has been reviewed by our system."
        )

    body = f"""
      <p style="color:#1e293b;font-size:16px;margin:0 0 12px;">Hi {name},</p>
      <p style="color:#475569;margin:0 0 24px;">
        {action_line}
        The recruiter will reach out if your profile is a good fit.
      </p>
      <p style="color:#64748b;font-size:13px;border-left:3px solid #e2e8f0;padding-left:12px;margin:0;">
        Irrespective of AI outcomes, the recruiter's decision is final.
      </p>
    """
    _send(to_email, subject, body)
