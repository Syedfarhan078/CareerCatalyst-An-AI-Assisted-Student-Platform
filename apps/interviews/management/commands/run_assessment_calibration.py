"""
Management command to run offline Stage 1-3 calibration benchmarks and print
either human-readable or machine-readable JSON calibration reports.

Never modifies candidate sessions, never consumes AI quota, and never creates
certificate records.
"""

import json
from django.core.management.base import BaseCommand

from apps.interviews.services.calibration import CalibrationRunner


class Command(BaseCommand):
    help = "Runs offline calibration checks across Stage 1-3 assessment evaluators (zero DB side effects)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output machine-readable JSON calibration report.',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Exit non-zero if any calibration case falls outside its expected range or gate.',
        )

    def handle(self, *args, **options):
        report = CalibrationRunner.run_suite()

        if options.get('json'):
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self.stdout.write(
                f"Calibration Report [{report['calibration_version']} @ {report['blueprint_version']}]"
            )
            self.stdout.write(
                f"  Total Cases: {report['total_cases']} "
                f"(S1={report['by_stage'][1]}, S2={report['by_stage'][2]}, S3={report['by_stage'][3]})"
            )
            self.stdout.write(
                f"  Review Status: PROVISIONAL={report['provisional_fixtures']}, "
                f"HUMAN_REVIEWED={report['human_reviewed_fixtures']}"
            )
            self.stdout.write(
                f"  Passed Expectations: {report['cases_passing_expectations']} / {report['total_cases']} "
                f"(Outside Range/Gate: {report['cases_outside_expectations']})"
            )
            self.stdout.write(
                f"  False Passes: {len(report['false_passes'])} | "
                f"False Fails: {len(report['false_fails'])} | "
                f"Critical Gate Mismatches: {report['critical_gate_mismatches']}"
            )
            for lb in report['length_bias_checks']:
                self.stdout.write(
                    f"  Length-Bias [{lb['concise_case']} vs {lb['verbose_case']}]: "
                    f"delta={lb['score_delta']} (within_limit={lb['within_threshold']})"
                )
            for row in report['case_results']:
                status_lbl = "PASS" if row['passed_expectation'] else "FAIL"
                reasons = f" -> {'; '.join(row['failure_reasons'])}" if row['failure_reasons'] else ""
                self.stdout.write(
                    f"  [{status_lbl}] {row['case_id']} (S{row['stage']} {row['review_status']}): "
                    f"score={row['observed_score']}, gate={row['observed_gate']}{reasons}"
                )

        if options.get('strict') and report['cases_outside_expectations'] > 0:
            raise SystemExit(1)
