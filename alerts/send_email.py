import smtplib
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_digest_email(subject, body, to_addr, from_addr, app_password):
    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(from_addr, app_password)
        server.sendmail(from_addr, [to_addr], message.as_string())
