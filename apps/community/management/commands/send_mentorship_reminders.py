from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.community.emails import send_session_day_reminders


class Command(BaseCommand):
    help = "Dispatches day-of-session email reminders to mentors and students for today's scheduled 1-on-1 mentorship meetings."

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            default=None,
            help="Target date for sessions to remind in YYYY-MM-DD format (defaults to today)."
        )

    def handle(self, *args, **options):
        date_str = options.get('date')
        target_date = None

        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                self.stderr.write(self.style.ERROR(f"Invalid date format '{date_str}'. Expected YYYY-MM-DD."))
                return

        if target_date is None:
            target_date = timezone.now().date()

        self.stdout.write(f"Checking for mentorship sessions on {target_date}...")
        sent_count = send_session_day_reminders(target_date=target_date)

        if sent_count > 0:
            self.stdout.write(self.style.SUCCESS(
                f"Successfully dispatched day-of-session reminders for {sent_count} mentorship booking(s)."
            ))
        else:
            self.stdout.write(self.style.NOTICE(
                f"No pending session reminders due for {target_date}."
            ))
