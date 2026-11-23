from django.urls import path
from . import views

urlpatterns = [
    path("", views.home_view, name="home"),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('career-guides/', views.guide_list_view, name='guide_list'),
    path('career-guides/<slug:slug>/', views.guide_detail_view, name='guide_detail'),

    # Trust, Compliance, Legal & Company Foundation Pages
    path('about/', views.about_view, name='about_us'),
    path('privacy/', views.privacy_view, name='privacy_policy'),
    path('terms/', views.terms_view, name='terms_of_service'),
    path('contact/', views.contact_view, name='contact'),
    path('cookies/', views.cookies_view, name='cookie_policy'),
    path('data-deletion/', views.data_deletion_view, name='data_deletion'),
    path('community-guidelines/', views.community_guidelines_view, name='community_guidelines'),
]
