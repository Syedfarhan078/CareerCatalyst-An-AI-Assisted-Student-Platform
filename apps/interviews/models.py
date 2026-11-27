import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone


class QuestionCategory(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Question Categories"

    def __str__(self):
        return self.name


class Question(models.Model):
    TYPE_CHOICES = [
        ('MCQ', 'Multiple Choice'),
        ('Coding', 'Coding Challenge'),
        ('STAR', 'STAR/Behavioral'),
    ]
    DIFFICULTY_CHOICES = [
        ('Easy', 'Easy'),
        ('Medium', 'Medium'),
        ('Hard', 'Hard'),
    ]
    category = models.ForeignKey(QuestionCategory, on_delete=models.CASCADE, related_name='questions')
    title = models.CharField(max_length=255)
    content = models.TextField()
    question_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='MCQ')
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, default='Medium')
    
    # MCQ fields
    options = models.JSONField(null=True, blank=True, help_text="List of choices: ['Option A', 'Option B', ...]")
    correct_option = models.CharField(max_length=10, null=True, blank=True, help_text="e.g. A, B, C, or D")
    
    # Coding fields
    starter_code = models.TextField(blank=True, default='', help_text="Initial starter template code for candidates.")
    entry_point = models.CharField(max_length=100, blank=True, default='', help_text="Primary function name to evaluate.")
    test_cases = models.JSONField(null=True, blank=True, help_text="List of public test cases: [{'input': '...', 'expected': '...', 'function': '...'}]")
    hidden_test_cases = models.JSONField(null=True, blank=True, default=list, help_text="List of hidden test cases evaluated only on submission.")
    time_limit_ms = models.IntegerField(default=2000, help_text="Execution time limit in milliseconds.")
    memory_limit_mb = models.IntegerField(default=128, help_text="Memory limit in megabytes.")
    sample_solution = models.TextField(null=True, blank=True)
    skills_evaluated = models.CharField(max_length=255, blank=True, default='', help_text="Comma-separated competencies tested by this question.")

    class Meta:
        indexes = [
            models.Index(fields=['category', 'question_type', 'difficulty']),
            models.Index(fields=['question_type', 'difficulty']),
        ]

    def __str__(self):
        return f"[{self.get_question_type_display()}] {self.title}"


class UserAttempt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='quiz_attempts')
    category = models.ForeignKey(QuestionCategory, on_delete=models.CASCADE, related_name='attempts')
    score = models.IntegerField(default=0)
    attempted_at = models.DateTimeField(auto_now_add=True)
    proctor_violations_count = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'attempted_at']),
            models.Index(fields=['category', 'attempted_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.category.name} ({self.score}%)"


class UserAttemptDetail(models.Model):
    attempt = models.ForeignKey(UserAttempt, on_delete=models.CASCADE, related_name='details')
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    user_answer = models.TextField()
    is_correct = models.BooleanField(default=False)
    
    # Enhanced execution and evaluation telemetry
    execution_time_ms = models.FloatField(null=True, blank=True)
    memory_used_kb = models.FloatField(null=True, blank=True)
    passed_tests = models.IntegerField(null=True, blank=True)
    total_tests = models.IntegerField(null=True, blank=True)
    test_results = models.JSONField(default=dict, blank=True)
    execution_status = models.CharField(max_length=50, blank=True, default='')

    def __str__(self):
        return f"{self.attempt} - {self.question.title}"


class QuizSession(models.Model):
    """
    Persistent server-managed quiz attempt state.
    Provides anti-clash multi-tab isolation, server-side countdown timestamps,
    and idempotent atomic submissions.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='quiz_sessions')
    category = models.ForeignKey(QuestionCategory, on_delete=models.CASCADE, related_name='quiz_sessions')
    session_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    is_submitted = models.BooleanField(default=False)
    score = models.IntegerField(null=True, blank=True)
    question_ids = models.JSONField(default=list, help_text="Ordered list of question IDs selected for this quiz session.")
    proctor_violations_count = models.IntegerField(default=0)
    attempt = models.OneToOneField(UserAttempt, null=True, blank=True, on_delete=models.SET_NULL, related_name='quiz_session')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'is_submitted']),
            models.Index(fields=['session_uuid']),
            models.Index(fields=['user', 'category', 'is_submitted']),
        ]

    def __str__(self):
        status = "Submitted" if self.is_submitted else "Active"
        return f"QuizSession({self.user.username}, {self.category.slug}, {status})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def remaining_seconds(self) -> int:
        if self.is_submitted:
            return 0
        rem = int((self.expires_at - timezone.now()).total_seconds())
        return max(0, rem)


class MockInterviewSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mock_interviews')
    role = models.CharField(max_length=100)
    career_path = models.ForeignKey('roadmaps.CareerPath', null=True, blank=True, on_delete=models.SET_NULL, related_name='mock_sessions')
    started_at = models.DateTimeField(auto_now_add=True)
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    overall_score = models.IntegerField(default=0)
    feedback = models.TextField(blank=True)
    proctor_violations_count = models.IntegerField(default=0)
    
    # State machine and rubric telemetry
    current_phase = models.CharField(max_length=30, default='INTRODUCTION')
    current_turn = models.IntegerField(default=0)
    dimension_scores = models.JSONField(default=dict, blank=True, help_text="Scores across technical, problem solving, role relevance, communication, structure")

    class Meta:
        indexes = [
            models.Index(fields=['user', 'started_at']),
            models.Index(fields=['user', 'is_completed']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.role} ({self.overall_score}%)"


class MockInterviewChat(models.Model):
    SENDER_CHOICES = [
        ('Interviewer', 'Interviewer'),
        ('Candidate', 'Candidate'),
    ]
    session = models.ForeignKey(MockInterviewSession, on_delete=models.CASCADE, related_name='chats')
    sender = models.CharField(max_length=15, choices=SENDER_CHOICES)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    phase = models.CharField(max_length=30, blank=True, default='')

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.sender}: {self.message[:30]}"


class ProctorLog(models.Model):
    SESSION_CHOICES = [
        ('Quiz', 'Quiz'),
        ('MockInterview', 'MockInterview'),
        ('Coding', 'Coding'),
    ]
    SOURCE_CHOICES = [
        ('CLIENT_REPORTED', 'Client Reported'),
        ('SERVER_VERIFIED', 'Server Verified'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='proctor_logs')
    session_type = models.CharField(max_length=15, choices=SESSION_CHOICES)
    session_id = models.IntegerField(db_index=True)
    violation_type = models.CharField(max_length=100)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='CLIENT_REPORTED')
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['session_type', 'session_id']),
            models.Index(fields=['user', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.session_type} ({self.violation_type})"


class InterviewCompetencyEvidence(models.Model):
    """
    Evidence record for skills demonstrated through interview preparation.
    Bridges interview performance cautiously into CareerCatalyst's learning loop.
    """
    SOURCE_CHOICES = [
        ('CODING_CHALLENGE', 'Coding Challenge'),
        ('TECHNICAL_QUIZ', 'Technical Quiz'),
        ('MOCK_INTERVIEW', 'Mock Interview'),
        ('BEHAVIORAL_STAR', 'Behavioral STAR'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='interview_evidence')
    skill_name = models.CharField(max_length=100, db_index=True)
    source_type = models.CharField(max_length=30, choices=SOURCE_CHOICES)
    score = models.IntegerField()
    confidence_weight = models.FloatField(default=1.0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'skill_name']),
            models.Index(fields=['user', 'created_at']),
        ]
        verbose_name_plural = "Interview Competency Evidence"

    def __str__(self):
        return f"{self.user.username} - {self.skill_name} ({self.score}%)"


# =============================================================================
# AI-Era Career Readiness Assessment System (Sprint 1 Foundation Models)
# =============================================================================

class RoleAssessmentBlueprint(models.Model):
    """
    Versioned career-role assessment configuration synced from version-controlled
    blueprint files. Controls archetype, per-stage timing/weights, conservative
    Stage 3 stopping rules, and competency thresholds for a CareerPath.
    """
    ARCHETYPE_CHOICES = [
        ('ENGINEERING', 'Engineering & Code Review'),
        ('DATA_ML', 'Data, ML & AI Lab'),
        ('CLOUD_SEC', 'Cloud & Security War Room'),
        ('PRODUCT_DESIGN', 'Product & Design Studio'),
    ]
    CALIBRATION_STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PILOT', 'Pilot Active'),
        ('CALIBRATED', 'Empirically Calibrated'),
        ('ACTIVE', 'Production Active'),
        ('RETIRED', 'Retired'),
    ]

    career_path = models.ForeignKey(
        'roadmaps.CareerPath',
        on_delete=models.CASCADE,
        related_name='assessment_blueprints'
    )
    version = models.CharField(max_length=32)
    task_bank_version = models.CharField(max_length=32)
    archetype = models.CharField(max_length=30, choices=ARCHETYPE_CHOICES)
    calibration_status = models.CharField(
        max_length=20,
        choices=CALIBRATION_STATUS_CHOICES,
        default='DRAFT'
    )
    is_active = models.BooleanField(default=False)
    is_pilot = models.BooleanField(default=True)

    passing_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('70.00'))
    borderline_low = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('67.00'))
    borderline_high = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('73.00'))

    stage_weights = models.JSONField(
        default=dict,
        help_text="Normalized stage weights summing to 1.0, e.g. {'1': 0.35, '2': 0.35, '3': 0.30}"
    )
    stage_durations = models.JSONField(
        default=dict,
        help_text="Certification stage durations in seconds, e.g. {'1': 900, '2': 900, '3': 720}"
    )
    stopping_rules = models.JSONField(
        default=dict,
        help_text="Conservative Stage 3 stopping policy (min/max turns, competency coverage requirement)."
    )
    competency_definitions = models.JSONField(
        default=list,
        help_text="Ordered list of weighted competencies, critical gate floors, and observable rubric descriptors."
    )
    calibration_metadata = models.JSONField(default=dict, blank=True)
    spec_checksum = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['career_path__name', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['career_path', 'version'],
                name='uniq_blueprint_career_version'
            ),
            models.UniqueConstraint(
                fields=['career_path'],
                condition=Q(is_active=True),
                name='uniq_one_active_blueprint_per_role'
            ),
        ]
        indexes = [
            models.Index(fields=['career_path', 'is_active']),
            models.Index(fields=['archetype', 'calibration_status']),
        ]

    def __str__(self):
        active_flag = "ACTIVE" if self.is_active else self.calibration_status
        return f"{self.career_path.name} ({self.version}) [{active_flag}]"


class AssessmentSession(models.Model):
    """
    Server-authoritative state machine for one Practice or Certification assessment run.
    """
    MODE_CHOICES = [
        ('PRACTICE', 'Practice Mode'),
        ('CERTIFICATION', 'Certification Assessment'),
    ]
    SCOPE_CHOICES = [
        ('FULL', 'Full Three-Stage Assessment'),
        ('STAGE_1', 'Stage 1 Only'),
        ('STAGE_2', 'Stage 2 Only'),
        ('STAGE_3', 'Stage 3 Only'),
    ]
    STATUS_CHOICES = [
        ('CREATED', 'Created / Pending Policy Consent'),
        ('STAGE_1', 'In Progress: Stage 1'),
        ('STAGE_2', 'In Progress: Stage 2'),
        ('STAGE_3', 'In Progress: Stage 3'),
        ('PAUSED_OUTAGE', 'Paused Due to Provider Outage'),
        ('SUBMITTED', 'Submitted / Awaiting Evaluation'),
        ('EVALUATING', 'Evaluating'),
        ('PENDING_EVALUATION', 'Pending Delayed AI Evaluation'),
        ('EVALUATED', 'Evaluated'),
        ('EXPIRED', 'Timer Expired Before Completion'),
        ('CANCELLED', 'Cancelled'),
    ]

    ACTIVE_CERT_STATUSES = [
        'CREATED', 'STAGE_1', 'STAGE_2', 'STAGE_3',
        'PAUSED_OUTAGE', 'SUBMITTED', 'EVALUATING', 'PENDING_EVALUATION',
    ]

    session_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='assessment_sessions'
    )
    career_path = models.ForeignKey(
        'roadmaps.CareerPath',
        on_delete=models.PROTECT,
        related_name='assessment_sessions'
    )
    blueprint = models.ForeignKey(
        RoleAssessmentBlueprint,
        on_delete=models.PROTECT,
        related_name='sessions'
    )
    blueprint_version = models.CharField(max_length=32)
    task_bank_version = models.CharField(max_length=32)
    task_bundle_id = models.CharField(max_length=64, db_index=True)

    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default='PRACTICE')
    practice_scope = models.CharField(max_length=15, choices=SCOPE_CHOICES, default='FULL')
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='CREATED', db_index=True)
    current_stage = models.PositiveSmallIntegerField(default=1)

    extended_time_enabled = models.BooleanField(default=False)
    time_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('1.00'))

    policy_accepted_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    stage_started_at = models.DateTimeField(null=True, blank=True)
    stage_expires_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_from_status = models.CharField(max_length=25, blank=True, default='')
    accumulated_pause_seconds = models.PositiveIntegerField(default=0)
    reconnect_grace_used_stages = models.JSONField(default=list, blank=True)

    submitted_at = models.DateTimeField(null=True, blank=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    overall_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)

    rubric_snapshot = models.JSONField(default=list, blank=True)
    integrity_summary = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'career_path'],
                condition=Q(
                    mode='CERTIFICATION',
                    status__in=[
                        'CREATED', 'STAGE_1', 'STAGE_2', 'STAGE_3',
                        'PAUSED_OUTAGE', 'SUBMITTED', 'EVALUATING', 'PENDING_EVALUATION',
                    ],
                ),
                name='uniq_active_cert_session_per_user_role'
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'career_path', 'mode', 'status']),
            models.Index(fields=['user', 'mode', 'created_at']),
            models.Index(fields=['user', 'cooldown_until']),
        ]

    def __str__(self):
        return f"AssessmentSession({self.user.username}, {self.career_path.slug}, {self.mode}, {self.status})"


class AssessmentStageResponse(models.Model):
    """
    Stores frozen task snapshots, candidate submissions, Part A lock state,
    and deterministic evaluation output per stage.
    """
    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.CASCADE,
        related_name='stage_responses'
    )
    stage_number = models.PositiveSmallIntegerField()
    task_id = models.CharField(max_length=64)
    task_snapshot = models.JSONField(default=dict)
    response_payload = models.JSONField(default=dict, blank=True)
    part_a_submitted_at = models.DateTimeField(null=True, blank=True)
    deterministic_result = models.JSONField(default=dict, blank=True)
    draft_saved_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    is_locked = models.BooleanField(default=False)

    class Meta:
        ordering = ['stage_number']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'stage_number'],
                name='uniq_stage_response_per_session'
            ),
        ]
        indexes = [
            models.Index(fields=['session', 'stage_number', 'is_locked']),
        ]

    def __str__(self):
        return f"{self.session.session_uuid} - Stage {self.stage_number} ({'Locked' if self.is_locked else 'Open'})"


class AdaptiveInterviewTurn(models.Model):
    """
    Bounded conversational turn log for Stage 3 adaptive ownership defense.
    """
    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.CASCADE,
        related_name='interview_turns'
    )
    turn_number = models.PositiveSmallIntegerField()
    target_competencies = models.JSONField(default=list, blank=True)
    interviewer_prompt = models.TextField()
    twist_injected = models.BooleanField(default=False)
    context_summary_used = models.TextField(blank=True, default='')
    candidate_reply = models.TextField(blank=True, default='')
    turn_analysis = models.JSONField(default=dict, blank=True)

    provider_name = models.CharField(max_length=32, blank=True, default='')
    provider_model = models.CharField(max_length=64, blank=True, default='')
    tokens_used = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['turn_number']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'turn_number'],
                name='uniq_turn_per_session'
            ),
        ]

    def __str__(self):
        return f"{self.session.session_uuid} - Turn {self.turn_number}"


class EvaluationResult(models.Model):
    """
    Immutable authoritative audit record for an evaluated AssessmentSession,
    preserving full rubric snapshot, deterministic/AI evidence, and audit trail.
    """
    SCORING_MODE_CHOICES = [
        ('HYBRID_CERTIFICATION', 'Hybrid Certification Evaluation'),
        ('AI_PRACTICE', 'AI-Assisted Practice Evaluation'),
        ('DETERMINISTIC_PRACTICE', 'Deterministic Practice Fallback'),
    ]

    session = models.OneToOneField(
        AssessmentSession,
        on_delete=models.CASCADE,
        related_name='evaluation_result'
    )
    blueprint_version = models.CharField(max_length=32)
    task_bank_version = models.CharField(max_length=32)
    scoring_mode = models.CharField(max_length=30, choices=SCORING_MODE_CHOICES)

    weighted_total = models.DecimalField(max_digits=5, decimal_places=2)
    passing_threshold = models.DecimalField(max_digits=5, decimal_places=2)
    stage_scores = models.JSONField(default=dict)
    critical_gates_passed = models.BooleanField(default=False)
    passed = models.BooleanField(default=False)

    evaluator_confidence = models.FloatField(default=0.0)
    borderline_cross_checked = models.BooleanField(default=False)
    needs_human_review = models.BooleanField(default=False)
    review_reason = models.CharField(max_length=255, blank=True, default='')

    rubric_snapshot = models.JSONField(default=list)
    scoring_evidence = models.JSONField(default=dict)
    raw_ai_output = models.JSONField(default=dict, blank=True)
    remediation_recommendations = models.JSONField(default=list, blank=True)
    audit_trail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        verdict = "PASS" if self.passed else "FAIL"
        return f"Eval({self.session.session_uuid}): {self.weighted_total}% [{verdict}]"


class CompetencyScore(models.Model):
    """
    Normalized per-competency breakdown enabling remediation mapping, calibration
    audits, and career-readiness analytics without parsing JSON blobs.
    """
    evaluation = models.ForeignKey(
        EvaluationResult,
        on_delete=models.CASCADE,
        related_name='competency_scores'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='competency_scores'
    )
    career_path = models.ForeignKey(
        'roadmaps.CareerPath',
        on_delete=models.PROTECT,
        related_name='competency_scores'
    )
    competency_code = models.CharField(max_length=64)
    competency_name = models.CharField(max_length=120)
    weight = models.DecimalField(max_digits=4, decimal_places=2)
    is_critical = models.BooleanField(default=False)
    critical_floor = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    score = models.DecimalField(max_digits=5, decimal_places=2)
    deterministic_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    ai_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    gate_passed = models.BooleanField(default=True)

    rubric_criteria_snapshot = models.JSONField(default=dict, blank=True)
    reasoning = models.TextField(blank=True, default='')
    evidence_quotes = models.JSONField(default=list, blank=True)
    recommended_topic_ids = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['evaluation', 'competency_code'],
                name='uniq_competency_per_evaluation'
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'career_path', 'competency_code']),
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.competency_code}: {self.score}%"


class AIUsageLog(models.Model):
    """
    Cost-control, quota enforcement, and provider health ledger for AI gateway calls.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_usage_logs'
    )
    session = models.ForeignKey(
        AssessmentSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_usage_logs'
    )
    operation = models.CharField(max_length=40, db_index=True)
    provider = models.CharField(max_length=32)
    model_name = models.CharField(max_length=80, blank=True, default='')
    success = models.BooleanField(default=False)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'operation', 'created_at']),
            models.Index(fields=['provider', 'success', 'created_at']),
        ]

    def __str__(self):
        status = "OK" if self.success else f"ERR:{self.error_code or 'fail'}"
        return f"{self.operation} [{self.provider}/{self.model_name}] ({status})"


class ReadinessUserPreference(models.Model):
    """
    Persists per-user accessibility settings (+25% extended time) and explicit
    assessment policy consent timestamps without modifying profile models in
    unrelated apps.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='readiness_preference'
    )
    extended_time_enabled = models.BooleanField(default=False)
    last_consent_version = models.CharField(max_length=32, blank=True, default='')
    last_consented_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ReadinessPreference({self.user.username}, ext={self.extended_time_enabled})"
