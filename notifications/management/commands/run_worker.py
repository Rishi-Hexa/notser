"""Background delivery worker.

Polls for due deliveries (PENDING, or RETRYING whose time has come) and sends
them, applying the retry ladder / dead-lettering. This is the DB-backed stand-in
for the Kafka consumer in the design doc — same producer/consumer split, no
extra infrastructure.

Usage:
    python manage.py run_worker                       # loop forever, poll every 1s
    python manage.py run_worker --once                # process what's due, then exit
    python manage.py run_worker --label A --batch 1   # tagged output, one at a time
"""
import os
import time

from django.core.management.base import BaseCommand

from notifications import services


class Command(BaseCommand):
    help = "Process due notification deliveries (async send + retry + DLQ)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Process currently-due deliveries once and exit.")
        parser.add_argument("--interval", type=float, default=1.0,
                            help="Seconds to wait between polls (default 1.0).")
        parser.add_argument("--batch", type=int, default=50,
                            help="Max deliveries claimed per poll (default 50).")
        parser.add_argument("--label", default=None,
                            help="Worker name shown in output (default: pid).")

    def handle(self, *args, **options):
        once = options["once"]
        interval = options["interval"]
        batch = options["batch"]
        label = options["label"] or f"pid:{os.getpid()}"

        def run_pass():
            processed = services.process_due_deliveries(batch=batch)
            for delivery_id, priority, status in processed:
                self.stdout.write(f"[{label}] {priority:<6} {delivery_id} -> {status}")
            return len(processed)

        if once:
            count = run_pass()
            self.stdout.write(self.style.SUCCESS(f"[{label}] processed {count}."))
            return

        self.stdout.write(self.style.SUCCESS(
            f"[{label}] worker started (interval={interval}s, batch={batch}). Ctrl-C to stop."
        ))
        try:
            while True:
                run_pass()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING(f"[{label}] stopped."))
