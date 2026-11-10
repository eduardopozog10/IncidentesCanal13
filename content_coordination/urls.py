from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.decorators.cache import never_cache
from . import views

app_name = 'contents'
urlpatterns = [
    path('login/', never_cache(auth_views.LoginView.as_view(template_name='content_coordination/login.html', next_page='contents:dashboard')), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='contents:login'), name='logout'),
    path('', views.dashboard, name='dashboard'),
    path('data/', views.fragment, name='data'),
    path('send/', views.send, name='send'),
    path('<int:pk>/', views.dashboard, name='detail'),
    path('<int:pk>/data/', views.fragment, name='detail-data'),
]
