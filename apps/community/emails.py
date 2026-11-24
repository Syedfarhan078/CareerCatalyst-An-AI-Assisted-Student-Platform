import logging
import os
import random
import string
import uuid
import requests
from datetime import datetime, date, time
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

logger = logging.getLogger(__name__)


def generate_google_meet_link():
    """
    Auto-generates a Google Meet link matching the standard format:
    https://meet.google.com/xxx-yyyy-zzz (lowercase alphabetic letters).
    """
    part1 = "".join(random.choices(string.ascii_lowercase, k=3))
    part2 = "".join(random.choices(string.ascii_lowercase, k=4))
    part3 = "".join(random.choices(string.ascii_lowercase, k=3))
    return f"https://meet.google.com/{part1}-{part2}-{part3}"


def ensure_room_token(request_obj):
    """
    Ensures that a MentorshipRequest has a secure unique room token for its video room.
    """
    if not request_obj.room_token:
        request_obj.room_token = uuid.uuid4().hex[:12]
        try:
            request_obj.save(update_fields=['room_token'])
        except Exception:
            pass
    return request_obj.room_token


def get_jitsi_meet_room_name(request_obj):
    """
    Returns the deterministic, secure Jitsi Meet room name for a mentorship session:
    CareerCatalyst-Mentorship-{request_id}-{room_token}
    """
    token = ensure_room_token(request_obj)
    return f"CareerCatalyst-Mentorship-{request_obj.id}-{token}"


def get_jitsi_meet_url(request_obj):
    """
    Returns the direct underlying Jitsi Meet URL:
    https://meet.jit.si/CareerCatalyst-Mentorship-{id}-{token}
    """
    room_name = get_jitsi_meet_room_name(request_obj)
    return f"https://meet.jit.si/{room_name}"


def get_session_room_gateway_url(request_obj):
    """
    Returns the full canonical CareerCatalyst room gateway URL:
    https://<canonical_origin>/community/mentorship/room/<request_id>/
    This gateway strictly enforces:
    - User authentication and authorization (only student and mentor)
    - 5-minute pre-session countdown waiting room (cannot join early)
    - Active embedded Jitsi Meet video session
    - Automatic deactivation after session concludes (45m + 15m grace)
    """
    try:
        path = reverse('community:mentorship_room', args=[request_obj.id])
    except Exception:
        path = f"/community/mentorship/room/{request_obj.id}/"
    return f"{get_canonical_origin()}{path}"


def get_canonical_origin():
    """Returns canonical base origin for email action links."""
    protocol = getattr(settings, 'CANONICAL_PROTOCOL', 'https')
    hostname = getattr(settings, 'CANONICAL_HOSTNAME', None) or 'career-catalyst-ad5m.onrender.com'
    return f"{protocol}://{hostname}"


def _format_date(val):
    """Safely formats a date or date-string into 'Monday, November 30, 2026'."""
    if not val:
        return "Scheduled Date"
    if isinstance(val, (date, datetime)):
        return val.strftime('%A, %B %d, %Y')
    val_str = str(val).strip()
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(val_str, fmt).strftime('%A, %B %d, %Y')
        except ValueError:
            pass
    return val_str


def _format_time(val):
    """Safely formats a time or time-string into '10:00 AM'."""
    if not val:
        return "Scheduled Time"
    if isinstance(val, (time, datetime)):
        return val.strftime('%I:%M %p')
    val_str = str(val).strip()
    for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p', '%I:%M%p', '%I %p'):
        try:
            return datetime.strptime(val_str, fmt).strftime('%I:%M %p')
        except ValueError:
            pass
    return val_str


def send_email_message(to_email, to_name, subject, text_content, html_content=None):
    """
    Dispatches transactional email.
    1. Uses Brevo HTTP API if BREVO_API_KEY is configured (bypassing cloud SMTP restrictions).
    2. Falls back to Django's standard configured mailer.
    3. Traps exceptions so network/email transport hiccups do not abort parent database transactions.
    """
    if not to_email:
        logger.warning("send_email_message called without recipient email address.")
        return False

    brevo_api_key = os.getenv('BREVO_API_KEY')
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'supportatcareercatalyst@gmail')

    if brevo_api_key:
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "api-key": brevo_api_key,
            "content-type": "application/json"
        }
        payload = {
            "sender": {
                "email": from_email,
                "name": "Career Catalyst"
            },
            "to": [{"email": to_email, "name": to_name or to_email}],
            "subject": subject,
            "textContent": text_content,
        }
        if html_content:
            payload["htmlContent"] = html_content

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Email sent via Brevo HTTP API to %s: %s", to_email, subject)
            return True
        except Exception as e:
            logger.warning("Brevo API delivery failed (%s), falling back to Django mailer.", e)

    # Standard Django mailer fallback
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[to_email]
        )
        if html_content:
            msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=True)
        logger.info("Email sent via Django mail backend to %s: %s", to_email, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, e)
        return False


def send_booking_notification_to_mentor(request_obj):
    """
    Sends an email notification to the mentor when a student requests a session.
    Includes student background, requested date/time, purpose, and review link.
    """
    mentor = request_obj.mentor
    mentor_email = mentor.email or (mentor.user.email if mentor.user else None)
    if not mentor_email:
        return False

    student = request_obj.student
    student_name = student.get_full_name() or student.username
    student_profile = getattr(student, 'studentprofile', None)
    college = student_profile.college if student_profile and student_profile.college else "Not Specified"
    career_goal = student_profile.career_goal if student_profile and student_profile.career_goal else (student_profile.preferred_domain if student_profile else "Not Specified")

    formatted_date = _format_date(request_obj.requested_date)
    formatted_time = _format_time(request_obj.requested_time)
    review_url = f"{get_canonical_origin()}{reverse('community:mentor_requests')}"

    subject = f"[CareerCatalyst] New Mentorship Booking Request from {student_name}"

    text_content = (
        f"Hello {mentor.full_name},\n\n"
        f"You have received a new 1-on-1 mentorship session request on CareerCatalyst!\n\n"
        f"--- Candidate Details ---\n"
        f"Candidate Name: {student_name}\n"
        f"Email: {student.email}\n"
        f"College: {college}\n"
        f"Target Goal: {career_goal}\n\n"
        f"--- Session Details ---\n"
        f"Purpose: {request_obj.purpose}\n"
        f"Date: {formatted_date}\n"
        f"Time: {formatted_time} (45 minutes)\n\n"
        f"Candidate Message:\n"
        f"\"{request_obj.student_message}\"\n\n"
        f"Please log in to your Mentor Workspace to accept or decline this session:\n"
        f"{review_url}\n\n"
        f"Tip: When you accept, a Google Meet link will automatically be generated and sent to {student_name} and your inbox.\n\n"
        f"Best regards,\n"
        f"The CareerCatalyst Mentorship Team"
    )

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #2563eb; padding: 24px 32px; color: #ffffff; }}
            .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
            .content {{ padding: 32px; }}
            .card {{ background: #f1f5f9; border-radius: 8px; padding: 18px; margin: 20px 0; border: 1px solid #cbd5e1; }}
            .card-title {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #475569; margin-bottom: 10px; }}
            .field-row {{ margin-bottom: 6px; font-size: 14px; }}
            .field-label {{ font-weight: 600; color: #334155; }}
            .message-box {{ background: #ffffff; border-left: 3px solid #2563eb; padding: 12px 16px; margin-top: 12px; font-style: italic; border-radius: 0 6px 6px 0; }}
            .btn {{ display: inline-block; background: #2563eb; color: #ffffff !important; text-decoration: none; padding: 12px 24px; border-radius: 6px; font-weight: 600; margin-top: 20px; font-size: 14px; }}
            .footer {{ background: #f8fafc; padding: 16px 32px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>CareerCatalyst Mentorship</h1>
            </div>
            <div class="content">
                <p>Hello <strong>{escape(mentor.full_name)}</strong>,</p>
                <p>You have received a new 1-on-1 mentorship session request from an aspiring engineering candidate!</p>

                <div class="card">
                    <div class="card-title">Candidate Profile</div>
                    <div class="field-row"><span class="field-label">Name:</span> {escape(student_name)}</div>
                    <div class="field-row"><span class="field-label">Email:</span> {escape(student.email)}</div>
                    <div class="field-row"><span class="field-label">College:</span> {escape(college)}</div>
                    <div class="field-row"><span class="field-label">Career Target:</span> {escape(career_goal)}</div>
                </div>

                <div class="card">
                    <div class="card-title">Requested Session Details</div>
                    <div class="field-row"><span class="field-label">Purpose:</span> <strong>{escape(request_obj.purpose)}</strong></div>
                    <div class="field-row"><span class="field-label">Date:</span> {escape(formatted_date)}</div>
                    <div class="field-row"><span class="field-label">Time:</span> {escape(formatted_time)} (45 mins)</div>
                    <div class="field-label" style="margin-top: 10px;">Candidate Message:</div>
                    <div class="message-box">"{escape(request_obj.student_message)}"</div>
                </div>

                <p style="margin-top: 20px;">
                    <a href="{review_url}" class="btn">Review &amp; Accept Request</a>
                </p>
                <p style="font-size: 13px; color: #64748b; margin-top: 15px;">
                    <em>Note: When you accept, a Google Meet link will automatically be generated and sent to both you and the candidate.</em>
                </p>
            </div>
            <div class="footer">
                CareerCatalyst &bull; Atria Institute of Technology, Bangalore &bull; Mentorship Network
            </div>
        </div>
    </body>
    </html>
    """

    return send_email_message(mentor_email, mentor.full_name, subject, text_content, html_content)


def send_booking_accepted_to_student(request_obj):
    """
    Sends an email notification to the student when their mentor accepts the session.
    Provides confirmed date/time, mentor note, session guidelines, and the secure video room link.
    """
    student = request_obj.student
    student_email = student.email
    if not student_email:
        return False

    student_name = student.get_full_name() or student.username
    mentor = request_obj.mentor

    session_date = request_obj.meeting_date or request_obj.requested_date
    session_time = request_obj.meeting_time or request_obj.requested_time
    formatted_date = _format_date(session_date)
    formatted_time = _format_time(session_time)
    room_url = get_session_room_gateway_url(request_obj)
    my_bookings_url = f"{get_canonical_origin()}{reverse('community:student_bookings')}"

    custom_external_link = None
    if request_obj.meeting_link and ('meet.google.com' in request_obj.meeting_link or 'zoom.us' in request_obj.meeting_link or 'teams.microsoft.com' in request_obj.meeting_link):
        custom_external_link = request_obj.meeting_link

    mentor_note_html = ""
    if request_obj.mentor_response:
        mentor_note_html = f"""
        <div class="field-label" style="margin-top: 10px;">Mentor Preparation Note:</div>
        <div class="note-box">"{escape(request_obj.mentor_response)}"</div>
        """

    custom_link_text = f"--- Mentor Custom Video Link ---\n{custom_external_link}\n\n" if custom_external_link else ""
    custom_link_html = f"""
    <div style="margin-top: 10px; padding: 10px; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px;">
        <span style="font-size: 12px; color: #475569;">Mentor's Direct Meeting Link:</span><br>
        <a href="{custom_external_link}" style="color: #2563eb; font-weight: 600; font-size: 13px;">{custom_external_link}</a>
    </div>
    """ if custom_external_link else ""

    subject = f"[CareerCatalyst] Mentorship Session Confirmed with {mentor.full_name}"

    text_content = (
        f"Hello {student_name},\n\n"
        f"Great news! Your mentorship session request has been ACCEPTED by {mentor.full_name} ({mentor.designation} at {mentor.company}).\n\n"
        f"--- Confirmed Session Details ---\n"
        f"Mentor: {mentor.full_name} ({mentor.company})\n"
        f"Purpose: {request_obj.purpose}\n"
        f"Date: {formatted_date}\n"
        f"Time: {formatted_time} (45 minutes)\n\n"
        f"--- Virtual Meeting Room ---\n"
        f"{room_url}\n\n"
        f"{custom_link_text}"
        f"* Room Access Rules: The video room unlocks 5 minutes before your scheduled start time. "
        f"The session runs for 45 minutes, after which the room automatically deactivates.\n\n"
        f"Mentor's Prep Note:\n"
        f"\"{request_obj.mentor_response or 'Looking forward to our discussion!'}\"\n\n"
        f"Please test your microphone and camera beforehand and have your resume or questions ready.\n\n"
        f"View your booking status and session history anytime here:\n"
        f"{my_bookings_url}\n\n"
        f"Best regards,\n"
        f"The CareerCatalyst Mentorship Team"
    )

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #16a34a; padding: 24px 32px; color: #ffffff; }}
            .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
            .content {{ padding: 32px; }}
            .card {{ background: #f0fdf4; border-radius: 8px; padding: 20px; margin: 20px 0; border: 1px solid #bbf7d0; }}
            .card-title {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #15803d; margin-bottom: 12px; }}
            .field-row {{ margin-bottom: 8px; font-size: 14px; }}
            .field-label {{ font-weight: 600; color: #166534; }}
            .meet-box {{ background: #ffffff; border: 2px dashed #16a34a; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center; }}
            .meet-btn {{ display: inline-block; background: #16a34a; color: #ffffff !important; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 700; font-size: 15px; margin-top: 8px; }}
            .note-box {{ background: #f8fafc; border-left: 3px solid #64748b; padding: 12px 16px; margin-top: 12px; font-style: italic; border-radius: 0 6px 6px 0; font-size: 13px; color: #334155; }}
            .access-rules {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 12px; font-size: 12px; color: #1e40af; margin-top: 12px; text-align: left; }}
            .footer {{ background: #f8fafc; padding: 16px 32px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Session Confirmed!</h1>
            </div>
            <div class="content">
                <p>Hello <strong>{escape(student_name)}</strong>,</p>
                <p>Great news! Your mentorship session has been accepted by <strong>{escape(mentor.full_name)}</strong> ({escape(mentor.designation)} at {escape(mentor.company)}).</p>

                <div class="card">
                    <div class="card-title">Session Details</div>
                    <div class="field-row"><span class="field-label">Topic / Purpose:</span> <strong>{escape(request_obj.purpose)}</strong></div>
                    <div class="field-row"><span class="field-label">Date:</span> {escape(formatted_date)}</div>
                    <div class="field-row"><span class="field-label">Time:</span> {escape(formatted_time)} (45 minutes)</div>
                    <div class="field-row"><span class="field-label">Mentor:</span> {escape(mentor.full_name)} ({escape(mentor.company)})</div>
                    {mentor_note_html}
                </div>

                <div class="meet-box">
                    <div style="font-weight: 700; color: #1e293b; font-size: 15px; margin-bottom: 4px;">Virtual Video Meeting Room</div>
                    <div style="font-size: 13px; color: #64748b; margin-bottom: 12px;">Click below to access your secure 1-on-1 video room:</div>
                    <a href="{room_url}" class="meet-btn">Enter Virtual Meeting Room</a>
                    <div style="margin-top: 10px; font-size: 12px; word-break: break-all; color: #15803d;">
                        <a href="{room_url}" style="color: #15803d;">{room_url}</a>
                    </div>
                    <div class="access-rules">
                        &bull; <strong>Early Access:</strong> The room unlocks 5 minutes before scheduled start time.<br>
                        &bull; <strong>Time-Gating:</strong> The link will show a live countdown before the session and will automatically deactivate after the session ends.
                    </div>
                </div>

                <p style="font-size: 13px; color: #64748b;">
                    Please test your microphone and camera before the session. You can manage or review this booking anytime from <a href="{my_bookings_url}">My Mentorship Sessions</a>.
                </p>
            </div>
            <div class="footer">
                CareerCatalyst &bull; Learn. Build. Innovate. &bull; <a href="{get_canonical_origin()}">careercatalyst.in</a>
            </div>
        </div>
    </body>
    </html>
    """

    return send_email_message(student_email, student_name, subject, text_content, html_content)


def send_booking_accepted_to_mentor(request_obj):
    """
    Sends a confirmation copy to the mentor so they also have the virtual room link
    and scheduled date/time recorded directly in their email inbox.
    """
    mentor = request_obj.mentor
    mentor_email = mentor.email or (mentor.user.email if mentor.user else None)
    if not mentor_email:
        return False

    student = request_obj.student
    student_name = student.get_full_name() or student.username

    session_date = request_obj.meeting_date or request_obj.requested_date
    session_time = request_obj.meeting_time or request_obj.requested_time
    formatted_date = _format_date(session_date)
    formatted_time = _format_time(session_time)
    room_url = get_session_room_gateway_url(request_obj)

    subject = f"[CareerCatalyst] Mentorship Session Scheduled with {student_name}"

    text_content = (
        f"Hello {mentor.full_name},\n\n"
        f"You have confirmed a 1-on-1 mentorship session with {student_name}!\n\n"
        f"--- Confirmed Session Details ---\n"
        f"Candidate: {student_name} ({student.email})\n"
        f"Purpose: {request_obj.purpose}\n"
        f"Date: {formatted_date}\n"
        f"Time: {formatted_time} (45 minutes)\n\n"
        f"--- Virtual Meeting Room ---\n"
        f"{room_url}\n\n"
        f"* Room Access Rules: The video room unlocks 5 minutes before your scheduled start time. "
        f"The link is active for 45 minutes and deactivates after the session concludes.\n\n"
        f"This meeting link has also been emailed directly to the candidate.\n\n"
        f"Best regards,\n"
        f"The CareerCatalyst Mentorship Team"
    )

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #2563eb; padding: 24px 32px; color: #ffffff; }}
            .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
            .content {{ padding: 32px; }}
            .card {{ background: #f8fafc; border-radius: 8px; padding: 18px; margin: 20px 0; border: 1px solid #e2e8f0; }}
            .field-row {{ margin-bottom: 6px; font-size: 14px; }}
            .field-label {{ font-weight: 600; color: #334155; }}
            .meet-box {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center; }}
            .meet-btn {{ display: inline-block; background: #2563eb; color: #ffffff !important; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 600; font-size: 14px; margin-top: 8px; }}
            .access-rules {{ background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 12px; font-size: 12px; color: #475569; margin-top: 12px; text-align: left; }}
            .footer {{ background: #f8fafc; padding: 16px 32px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Session Scheduled</h1>
            </div>
            <div class="content">
                <p>Hello <strong>{escape(mentor.full_name)}</strong>,</p>
                <p>Your session with <strong>{escape(student_name)}</strong> has been confirmed.</p>

                <div class="card">
                    <div class="field-row"><span class="field-label">Candidate:</span> {escape(student_name)} ({escape(student.email)})</div>
                    <div class="field-row"><span class="field-label">Purpose:</span> {escape(request_obj.purpose)}</div>
                    <div class="field-row"><span class="field-label">Date:</span> {escape(formatted_date)}</div>
                    <div class="field-row"><span class="field-label">Time:</span> {escape(formatted_time)} (45 mins)</div>
                </div>

                <div class="meet-box">
                    <div style="font-weight: 600; color: #1e293b; font-size: 14px; margin-bottom: 4px;">Virtual Video Room</div>
                    <div style="font-size: 13px; color: #64748b; margin-bottom: 12px;">Access your private mentorship room below:</div>
                    <a href="{room_url}" class="meet-btn">Enter Virtual Meeting Room</a>
                    <div style="margin-top: 8px; font-size: 12px; color: #2563eb;">
                        <a href="{room_url}">{room_url}</a>
                    </div>
                    <div class="access-rules">
                        &bull; <strong>Early Access:</strong> The room unlocks 5 minutes before scheduled start time.<br>
                        &bull; <strong>Time-Gating:</strong> The room automatically closes after the 45-minute session ends.
                    </div>
                </div>
            </div>
            <div class="footer">
                CareerCatalyst &bull; Mentor Workspace
            </div>
        </div>
    </body>
    </html>
    """

    return send_email_message(mentor_email, mentor.full_name, subject, text_content, html_content)


def send_session_day_reminder_to_student(request_obj):
    """
    Sends a day-of-meeting reminder email to the student with the room link and prep advice.
    """
    student = request_obj.student
    student_email = student.email
    if not student_email:
        return False

    student_name = student.get_full_name() or student.username
    mentor = request_obj.mentor

    session_date = request_obj.meeting_date or request_obj.requested_date
    session_time = request_obj.meeting_time or request_obj.requested_time
    formatted_date = _format_date(session_date)
    formatted_time = _format_time(session_time)
    room_url = get_session_room_gateway_url(request_obj)

    subject = f"[Reminder] Your Mentorship Session is TODAY with {mentor.full_name} at {formatted_time}"

    text_content = (
        f"Hello {student_name},\n\n"
        f"This is a friendly reminder that your 1-on-1 mentorship session is scheduled for TODAY!\n\n"
        f"--- Session Summary ---\n"
        f"Mentor: {mentor.full_name} ({mentor.designation} at {mentor.company})\n"
        f"Date: TODAY ({formatted_date})\n"
        f"Time: {formatted_time} (45 minutes)\n"
        f"Purpose: {request_obj.purpose}\n\n"
        f"--- Join Meeting Room ---\n"
        f"{room_url}\n\n"
        f"* Please note: The meeting room unlocks 5 minutes before your session start time.\n"
        f"* Be sure to test your microphone and camera, and have your resume or project questions open.\n\n"
        f"Best of luck with your session,\n"
        f"The CareerCatalyst Mentorship Team"
    )

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #0284c7; padding: 24px 32px; color: #ffffff; }}
            .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
            .content {{ padding: 32px; }}
            .card {{ background: #f0f9ff; border-radius: 8px; padding: 18px; margin: 20px 0; border: 1px solid #bae6fd; }}
            .field-row {{ margin-bottom: 6px; font-size: 14px; }}
            .field-label {{ font-weight: 600; color: #0369a1; }}
            .meet-box {{ background: #ffffff; border: 2px dashed #0284c7; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center; }}
            .meet-btn {{ display: inline-block; background: #0284c7; color: #ffffff !important; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 700; font-size: 15px; margin-top: 8px; }}
            .checklist {{ background: #f8fafc; border-left: 3px solid #0284c7; padding: 12px 16px; margin: 16px 0; font-size: 13px; color: #334155; }}
            .footer {{ background: #f8fafc; padding: 16px 32px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Session Reminder: TODAY at {escape(formatted_time)}</h1>
            </div>
            <div class="content">
                <p>Hello <strong>{escape(student_name)}</strong>,</p>
                <p>Your 1-on-1 mentorship session with <strong>{escape(mentor.full_name)}</strong> is happening <strong>TODAY</strong>!</p>

                <div class="card">
                    <div class="field-row"><span class="field-label">Mentor:</span> {escape(mentor.full_name)} ({escape(mentor.company)})</div>
                    <div class="field-row"><span class="field-label">Topic:</span> {escape(request_obj.purpose)}</div>
                    <div class="field-row"><span class="field-label">Time:</span> {escape(formatted_time)} (45 mins)</div>
                </div>

                <div class="meet-box">
                    <div style="font-weight: 700; color: #0f172a; font-size: 15px; margin-bottom: 4px;">Your Virtual Meeting Room</div>
                    <div style="font-size: 13px; color: #64748b; margin-bottom: 12px;">The room unlocks 5 minutes before {escape(formatted_time)}:</div>
                    <a href="{room_url}" class="meet-btn">Enter Meeting Room</a>
                    <div style="margin-top: 10px; font-size: 12px; color: #0284c7;">
                        <a href="{room_url}">{room_url}</a>
                    </div>
                </div>

                <div class="checklist">
                    <strong>Quick Preparation Checklist:</strong><br>
                    &bull; Test your microphone and camera ahead of time.<br>
                    &bull; Have your resume, project links, or discussion questions open.<br>
                    &bull; Be ready 5 minutes early to test room connectivity.
                </div>
            </div>
            <div class="footer">
                CareerCatalyst &bull; Learn. Build. Innovate. &bull; <a href="{get_canonical_origin()}">careercatalyst.in</a>
            </div>
        </div>
    </body>
    </html>
    """

    return send_email_message(student_email, student_name, subject, text_content, html_content)


def send_session_day_reminder_to_mentor(request_obj):
    """
    Sends a day-of-meeting reminder email to the mentor with candidate details and room link.
    """
    mentor = request_obj.mentor
    mentor_email = mentor.email or (mentor.user.email if mentor.user else None)
    if not mentor_email:
        return False

    student = request_obj.student
    student_name = student.get_full_name() or student.username

    session_date = request_obj.meeting_date or request_obj.requested_date
    session_time = request_obj.meeting_time or request_obj.requested_time
    formatted_date = _format_date(session_date)
    formatted_time = _format_time(session_time)
    room_url = get_session_room_gateway_url(request_obj)

    subject = f"[Reminder] Your Mentorship Session is TODAY with {student_name} at {formatted_time}"

    text_content = (
        f"Hello {mentor.full_name},\n\n"
        f"This is a friendly reminder that you have a 1-on-1 mentorship session scheduled for TODAY!\n\n"
        f"--- Session Summary ---\n"
        f"Candidate: {student_name} ({student.email})\n"
        f"Date: TODAY ({formatted_date})\n"
        f"Time: {formatted_time} (45 minutes)\n"
        f"Topic: {request_obj.purpose}\n\n"
        f"--- Join Meeting Room ---\n"
        f"{room_url}\n\n"
        f"* Please note: The meeting room unlocks 5 minutes before session start time.\n\n"
        f"Best regards,\n"
        f"The CareerCatalyst Mentorship Team"
    )

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1e293b; background: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #2563eb; padding: 24px 32px; color: #ffffff; }}
            .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
            .content {{ padding: 32px; }}
            .card {{ background: #f8fafc; border-radius: 8px; padding: 18px; margin: 20px 0; border: 1px solid #e2e8f0; }}
            .field-row {{ margin-bottom: 6px; font-size: 14px; }}
            .field-label {{ font-weight: 600; color: #334155; }}
            .meet-box {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center; }}
            .meet-btn {{ display: inline-block; background: #2563eb; color: #ffffff !important; text-decoration: none; padding: 12px 28px; border-radius: 6px; font-weight: 600; font-size: 14px; margin-top: 8px; }}
            .footer {{ background: #f8fafc; padding: 16px 32px; font-size: 12px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Session Reminder: TODAY at {escape(formatted_time)}</h1>
            </div>
            <div class="content">
                <p>Hello <strong>{escape(mentor.full_name)}</strong>,</p>
                <p>You have a 1-on-1 mentorship session with <strong>{escape(student_name)}</strong> scheduled for <strong>TODAY</strong>.</p>

                <div class="card">
                    <div class="field-row"><span class="field-label">Candidate:</span> {escape(student_name)} ({escape(student.email)})</div>
                    <div class="field-row"><span class="field-label">Topic:</span> {escape(request_obj.purpose)}</div>
                    <div class="field-row"><span class="field-label">Time:</span> {escape(formatted_time)} (45 mins)</div>
                </div>

                <div class="meet-box">
                    <div style="font-weight: 600; color: #1e293b; font-size: 14px; margin-bottom: 4px;">Virtual Video Room</div>
                    <div style="font-size: 13px; color: #64748b; margin-bottom: 12px;">Unlocks 5 minutes before scheduled start time:</div>
                    <a href="{room_url}" class="meet-btn">Enter Meeting Room</a>
                    <div style="margin-top: 8px; font-size: 12px; color: #2563eb;">
                        <a href="{room_url}">{room_url}</a>
                    </div>
                </div>
            </div>
            <div class="footer">
                CareerCatalyst &bull; Mentor Workspace
            </div>
        </div>
    </body>
    </html>
    """

    return send_email_message(mentor_email, mentor.full_name, subject, text_content, html_content)


def send_session_day_reminders(target_date=None):
    """
    Finds all Accepted mentorship sessions scheduled for target_date (defaults to today)
    that have not yet received day-of-session reminders, dispatches emails to both student
    and mentor, and records reminder_sent_at timestamp to avoid duplicate notifications.
    """
    from apps.community.models import MentorshipRequest
    from django.db.models import Q

    if target_date is None:
        target_date = timezone.now().date()

    eligible_requests = MentorshipRequest.objects.filter(
        status='Accepted',
        reminder_sent_at__isnull=True
    ).filter(
        Q(meeting_date=target_date) |
        Q(meeting_date__isnull=True, requested_date=target_date)
    ).select_related('student', 'mentor', 'mentor__user')

    sent_count = 0
    now = timezone.now()

    for req in eligible_requests:
        try:
            send_session_day_reminder_to_student(req)
            send_session_day_reminder_to_mentor(req)
            req.reminder_sent_at = now
            req.save(update_fields=['reminder_sent_at'])
            sent_count += 1
            logger.info("Sent session day reminder for MentorshipRequest ID %s to student and mentor.", req.id)
        except Exception as e:
            logger.error("Failed to send session reminder for MentorshipRequest ID %s: %s", req.id, e)

    return sent_count
