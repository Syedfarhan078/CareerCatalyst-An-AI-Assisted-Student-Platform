from django.urls import path
from . import views
from . import views_readiness
from . import views_stage1

app_name = 'interviews'

urlpatterns = [
    path('', views.InterviewHubView.as_view(), name='hub'),

    # AI-Era Career Readiness Hub Shell & Stage 1 Pilot (Feature-flagged behind ENABLE_READINESS_ASSESSMENT)
    path('readiness/roles/', views_readiness.CareerSelectorView.as_view(), name='readiness_roles'),
    path('readiness/role/<slug:slug>/', views_readiness.RolePracticeOverviewView.as_view(), name='readiness_role_overview'),
    path('readiness/role/<slug:slug>/policy/', views_readiness.readiness_policy_view, name='readiness_policy'),
    path('readiness/role/<slug:slug>/practice/stage-1/start/', views_stage1.stage1_practice_start, name='stage1_practice_start'),
    path('readiness/practice/<uuid:session_uuid>/stage-1/', views_stage1.stage1_workbench, name='stage1_workbench'),
    path('readiness/practice/<uuid:session_uuid>/stage-1/draft/', views_stage1.stage1_save_draft, name='stage1_save_draft'),
    path('readiness/practice/<uuid:session_uuid>/stage-1/abandon/', views_stage1.stage1_abandon, name='stage1_abandon'),
    path('readiness/practice/<uuid:session_uuid>/result/', views_stage1.stage1_result, name='stage1_result'),
    path('readiness/history/', views_readiness.PracticeHistoryView.as_view(), name='readiness_history'),
    
    # Quiz routes
    path('quiz/<slug:slug>/start/', views.start_quiz, name='start_quiz'),
    path('quiz/submit/', views.submit_quiz, name='submit_quiz'),
    
    # Coding routes
    path('coding/', views.CodingListView.as_view(), name='coding_list'),
    path('coding/<int:pk>/', views.CodingDetailView.as_view(), name='coding_detail'),
    path('coding/<int:pk>/submit/', views.submit_code, name='submit_code'),
    
    # HR & Behavioral STAR routes
    path('behavioral/', views.HRBehavioralListView.as_view(), name='behavioral_list'),
    path('behavioral/<int:pk>/', views.HRBehavioralDetailView.as_view(), name='behavioral_detail'),
    path('behavioral/<int:pk>/submit/', views.submit_star, name='submit_star'),
    
    # Mock interview routes
    path('mock/', views.MockInterviewListView.as_view(), name='mock_list'),
    path('mock/start/', views.start_mock, name='start_mock'),
    path('mock/<int:pk>/', views.mock_session, name='mock_session'),
    path('mock/<int:pk>/reply/', views.chat_reply, name='chat_reply'),
    path('mock/<int:pk>/report/', views.mock_report, name='mock_report'),
    
    # Proctor violation logger API
    path('api/proctor-log/', views.log_proctor_violation, name='log_proctor_violation'),
    
    # Combined performance report
    path('reports/', views.PerformanceReportView.as_view(), name='reports'),
]
