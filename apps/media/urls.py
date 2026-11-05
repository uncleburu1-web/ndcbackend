from django.urls import path

from . import views

urlpatterns = [path("media/upload-signature/", views.UploadSignature.as_view(), name="media-upload-signature")]
