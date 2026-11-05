from django.urls import path

from . import views as v

CASE_ACTIONS = ["claim", "release", "keep", "remove", "restore", "escalate", "warn", "pause", "lift-pause", "hold", "hide", "unhide"]

urlpatterns = [
    path("rules/", v.RulesView.as_view(), name="rules"),
    path("candidates/<slug:slug>/report/", v.CandidateReport.as_view(), name="candidate-report"),
    path("reports/status/", v.ReportStatusView.as_view(), name="report-status"),
    path("report/copyright/", v.CopyrightNotice.as_view(), name="copyright-notice"),
    path("me/reports/", v.MyReports.as_view(), name="my-reports"),
    path("me/moderation/", v.MyModeration.as_view(), name="my-moderation"),
    path("me/appeals/", v.MyAppeals.as_view(), name="my-appeals"),
    path("me/counter-notices/", v.MyCounterNotices.as_view(), name="my-counter-notices"),
    path("admin/moderation/cases/", v.AdminCases.as_view(), name="admin-moderation-cases"),
    path("admin/moderation/cases/<uuid:pk>/", v.AdminCaseDetail.as_view(), name="admin-moderation-case"),
    *[path(f"admin/moderation/cases/<uuid:pk>/{a}/",
           (v.AdminCaseHideAction if a in ("hide", "unhide") else v.AdminCaseAction).as_view(action_name=a),
           name=f"admin-moderation-case-{a}") for a in CASE_ACTIONS],
    path("admin/moderation/cases/<uuid:pk>/reports/<uuid:report_id>/reporter/", v.AdminRevealReporter.as_view(),
         name="admin-moderation-reporter"),
    path("admin/moderation/appeals/", v.AdminAppeals.as_view(), name="admin-moderation-appeals"),
    path("admin/moderation/appeals/<uuid:pk>/decide/", v.AdminAppealDecide.as_view(), name="admin-moderation-appeal-decide"),
    path("admin/moderation/legal/", v.AdminLegalNotices.as_view(), name="admin-moderation-legal"),
    path("admin/moderation/legal/<uuid:pk>/", v.AdminLegalNoticeDetail.as_view(), name="admin-moderation-legal-detail"),
    *[path(f"admin/moderation/legal/<uuid:pk>/{a}/", v.AdminLegalNoticeAction.as_view(action_name=a), name=f"admin-moderation-legal-{a}")
      for a in ("acknowledge", "hold", "owner-notified")],
    path("admin/moderation/legal/<uuid:pk>/attachment/", v.AdminLegalAttachment.as_view(), name="admin-moderation-legal-attachment"),
    path("admin/moderation/legal/<uuid:pk>/items/<uuid:item_id>/", v.AdminLegalNoticeItem.as_view(), name="admin-moderation-legal-item"),
    path("admin/moderation/compliance/", v.AdminCompliance.as_view(), name="admin-moderation-compliance"),
]
