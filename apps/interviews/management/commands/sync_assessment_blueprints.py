"""
Management command to validate and synchronize versioned RoleAssessmentBlueprints.
Supports --validate (schema + CareerPath + Topic integrity check) and --dry-run.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.interviews.blueprints import BlueprintValidationError
from apps.interviews.services.blueprint_loader import BlueprintRegistryService


class Command(BaseCommand):
    help = "Validates and synchronizes versioned role assessment blueprints into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            '--validate',
            action='store_true',
            help='Validate blueprint JSON files, DB CareerPaths, and Topic refs without writing to DB.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run full resolution simulation without committing RoleAssessmentBlueprint rows.'
        )
        parser.add_argument(
            '--strict-topics',
            action='store_true',
            help='Fail if any remediation_topic_ref cannot resolve to a seeded Topic when milestones exist.'
        )

    def handle(self, *args, **options):
        validate_only = options.get('validate', False)
        dry_run = options.get('dry_run', False)
        strict_topics = options.get('strict_topics', False)

        try:
            summary = BlueprintRegistryService.sync_all(
                dry_run=dry_run,
                validate_only=validate_only,
                strict_topics=strict_topics,
            )
        except BlueprintValidationError as exc:
            raise CommandError(f"Blueprint validation failed: {exc}") from exc

        for bp in summary['blueprints']:
            self.stdout.write(
                f"  [OK] {bp['role_slug']} @ {bp['version']} "
                f"({bp['bundles']} bundles, sha256:{bp['checksum'][:12]})"
            )

        for warn in summary['warnings']:
            self.stdout.write(self.style.WARNING(f"  [WARN] {warn}"))

        if validate_only:
            self.stdout.write(self.style.SUCCESS(
                f"Validation succeeded: {summary['validated_count']} blueprint(s) verified."
            ))
        elif dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY-RUN complete: {summary['validated_count']} blueprint(s) verified, 0 DB rows modified."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Sync complete: {summary['synced_count']} blueprint(s) synchronized."
            ))
