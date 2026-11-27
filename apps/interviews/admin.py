from django.contrib import admin
from .models import (
    QuestionCategory, Question, UserAttempt, UserAttemptDetail,
    MockInterviewSession, MockInterviewChat, ProctorLog,
    RoleAssessmentBlueprint, AssessmentSession, AssessmentStageResponse,
    AdaptiveInterviewTurn, EvaluationResult, CompetencyScore, AIUsageLog,
    ReadinessUserPreference,
)

class UserAttemptDetailInline(admin.TabularInline):
    model = UserAttemptDetail
    extra = 0

class MockInterviewChatInline(admin.TabularInline):
    model = MockInterviewChat
    extra = 0

@admin.register(QuestionCategory)
class QuestionCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'description')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'question_type', 'difficulty')
    list_filter = ('category', 'question_type', 'difficulty')
    search_fields = ('title', 'content')

@admin.register(UserAttempt)
class UserAttemptAdmin(admin.ModelAdmin):
    list_display = ('user', 'category', 'score', 'attempted_at', 'proctor_violations_count')
    list_filter = ('category', 'attempted_at')
    inlines = [UserAttemptDetailInline]

@admin.register(MockInterviewSession)
class MockInterviewSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'started_at', 'is_completed', 'overall_score', 'proctor_violations_count')
    list_filter = ('is_completed', 'started_at')
    inlines = [MockInterviewChatInline]

@admin.register(ProctorLog)
class ProctorLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'session_type', 'session_id', 'violation_type', 'timestamp')
    list_filter = ('session_type', 'violation_type', 'timestamp')


class AssessmentStageResponseInline(admin.TabularInline):
    model = AssessmentStageResponse
    extra = 0
    readonly_fields = ('stage_number', 'task_id', 'is_locked', 'submitted_at')


class CompetencyScoreInline(admin.TabularInline):
    model = CompetencyScore
    extra = 0
    readonly_fields = ('competency_code', 'score', 'is_critical', 'gate_passed')


@admin.register(RoleAssessmentBlueprint)
class RoleAssessmentBlueprintAdmin(admin.ModelAdmin):
    list_display = ('career_path', 'version', 'task_bank_version', 'archetype', 'calibration_status', 'is_active', 'passing_score')
    list_filter = ('archetype', 'calibration_status', 'is_active', 'is_pilot')
    search_fields = ('career_path__name', 'version', 'task_bank_version')
    readonly_fields = ('spec_checksum', 'created_at', 'updated_at')


@admin.register(AssessmentSession)
class AssessmentSessionAdmin(admin.ModelAdmin):
    list_display = ('session_uuid', 'user', 'career_path', 'mode', 'status', 'current_stage', 'overall_score', 'passed', 'created_at')
    list_filter = ('mode', 'status', 'passed', 'career_path')
    search_fields = ('session_uuid', 'user__username', 'task_bundle_id')
    readonly_fields = ('session_uuid', 'created_at', 'updated_at')
    inlines = [AssessmentStageResponseInline]


@admin.register(AdaptiveInterviewTurn)
class AdaptiveInterviewTurnAdmin(admin.ModelAdmin):
    list_display = ('session', 'turn_number', 'twist_injected', 'provider_name', 'tokens_used', 'created_at')
    list_filter = ('twist_injected', 'provider_name')


@admin.register(EvaluationResult)
class EvaluationResultAdmin(admin.ModelAdmin):
    list_display = ('session', 'scoring_mode', 'weighted_total', 'critical_gates_passed', 'passed', 'needs_human_review', 'created_at')
    list_filter = ('scoring_mode', 'passed', 'critical_gates_passed', 'needs_human_review')
    readonly_fields = ('created_at',)
    inlines = [CompetencyScoreInline]


@admin.register(CompetencyScore)
class CompetencyScoreAdmin(admin.ModelAdmin):
    list_display = ('user', 'career_path', 'competency_code', 'score', 'is_critical', 'gate_passed', 'created_at')
    list_filter = ('career_path', 'competency_code', 'is_critical', 'gate_passed')


@admin.register(AIUsageLog)
class AIUsageLogAdmin(admin.ModelAdmin):
    list_display = ('operation', 'provider', 'model_name', 'success', 'total_tokens', 'latency_ms', 'error_code', 'created_at')
    list_filter = ('operation', 'provider', 'success')
    readonly_fields = ('created_at',)


@admin.register(ReadinessUserPreference)
class ReadinessUserPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'extended_time_enabled', 'last_consent_version', 'last_consented_at', 'updated_at')
    search_fields = ('user__username',)
