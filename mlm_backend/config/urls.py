from django.conf import settings
from django.contrib import admin
from django.urls import path, re_path
from django.views.static import serve
from apps.admin_panel import views as adminv
from apps.agreements import views as agreement_views
from apps.authentication import views as auth_views
from apps.cart import views as cart_views
from apps.commissions import user_views as comm_views
from apps.commissions import admin_milestone_views as admin_ms_views
from apps.courses import views as course_views
from apps.banners import views as banner_views
from apps.payments import views as pay_views
from apps.sponsor_slots import views as slot_views
from apps.mlm_tree import admin_placement_views as mlm_placement_admin
from apps.mlm_tree import admin_binary_tree_views as mlm_tree_admin
from apps.mlm_tree import user_views as mlm_tree_user_views
from apps.users import member_views as user_views
from apps.wallet import views as wallet_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # Auth
    path("api/v1/auth/send-otp/", auth_views.send_otp),
    path("api/v1/auth/register/send-otp/", auth_views.send_register_otp),
    path("api/v1/auth/verify-otp-register/", auth_views.verify_otp_register),
    path("api/v1/auth/verify-otp-login/", auth_views.verify_otp_login),
    path("api/v1/auth/logout/", auth_views.logout),
    path("api/v1/auth/me/", auth_views.me),
    path("api/v1/auth/kyc/submit/", auth_views.kyc_submit),
    path("api/v1/auth/kyc/status/", auth_views.kyc_status),
    path("api/v1/auth/bank/", auth_views.bank_update),
    path("api/v1/auth/compliance/submit/", agreement_views.compliance_submit),
    path("api/v1/agreements/", agreement_views.legal_documents_public_list),
    path(
        "api/v1/agreements/compliance-legal/",
        agreement_views.legal_documents_compliance_legal_list,
    ),
    path("api/v1/agreements/send-otp/", agreement_views.agreement_send_otp),
    path("api/v1/agreements/verify/", agreement_views.agreement_verify),
    path(
        "api/v1/agreements/acceptance-proof/<uuid:acceptance_batch_id>/download/",
        agreement_views.agreement_acceptance_proof_download,
        name="agreement_acceptance_proof_download",
    ),
    path("api/v1/auth/validate-referral/", auth_views.validate_referral),
    # Admin auth
    path("api/v1/admin/auth/login/", auth_views.admin_password_login),
    path("api/v1/admin/auth/send-otp/", auth_views.admin_send_otp),
    path("api/v1/admin/auth/verify-otp/", auth_views.admin_verify_otp),
    # User — referral & tree
    path("api/v1/user/referral/", user_views.referral_me),
    path("api/v1/user/referral/stats/", user_views.referral_stats),
    path("api/v1/user/referral/list/", user_views.referral_list),
    path("api/v1/user/dashboard/", user_views.user_dashboard),
    path("api/v1/user/team/network/", user_views.team_network),
    path("api/v1/user/team/network/roster/", user_views.team_network_roster),
    path("api/v1/user/tree/", user_views.tree_me),
    path("api/v1/user/tree/subtree/", user_views.tree_subtree),
    path("api/v1/user/tree/uplines/", user_views.tree_uplines),
    path("api/v1/user/tree/downlines/", user_views.tree_downlines),
    path("api/v1/user/tree/level/<int:n>/", user_views.tree_level_n),
    path("api/v1/user/tree/place-direct/", mlm_tree_user_views.tree_place_direct),
    path("api/v1/admin/placements/pending/", mlm_placement_admin.admin_placements_pending),
    path(
        "api/v1/admin/placements/<int:order_id>/reverse/",
        mlm_placement_admin.admin_placement_reverse,
    ),
    path(
        "api/v1/admin/placements/<int:order_id>/reassign/",
        mlm_placement_admin.admin_placement_reassign,
    ),
    # Admin — Binary tree management (UI-oriented)
    path("api/v1/admin/binary-tree/dashboard/", mlm_tree_admin.admin_binary_tree_dashboard),
    path(
        "api/v1/admin/binary-tree/placements/pending/",
        mlm_tree_admin.admin_binary_tree_pending_placements,
    ),
    path("api/v1/admin/binary-tree/tree/", mlm_tree_admin.admin_binary_tree_tree_view),
    path("api/v1/admin/binary-tree/members/", mlm_tree_admin.admin_binary_tree_members_list),
    path(
        "api/v1/admin/binary-tree/weak-leg-report/",
        mlm_tree_admin.admin_binary_tree_weak_leg_report,
    ),
    path(
        "api/v1/admin/binary-tree/placements/<int:order_id>/place/",
        mlm_tree_admin.admin_binary_tree_place_under_parent,
    ),
    path("api/v1/admin/tree/user/<int:user_id>/", user_views.admin_tree_user),
    path("api/v1/admin/tree/platform/", user_views.admin_tree_platform),
    # Commissions & earnings bundles
    path("api/v1/user/earnings/", comm_views.user_earnings_bundle),
    path("api/v1/user/commissions/", comm_views.user_commissions),
    path("api/v1/user/commissions/summary/", comm_views.user_commissions_summary),
    path("api/v1/user/commissions/milestones/", comm_views.user_milestones),
    path("api/v1/user/commissions/tds/", comm_views.user_tds),
    path("api/v1/admin/commissions/", comm_views.admin_commissions),
    path("api/v1/admin/commissions/pending/", comm_views.admin_commissions_pending),
    path("api/v1/admin/commissions/force-credit/", comm_views.admin_force_credit),
    path("api/v1/admin/commissions/tds-report/", comm_views.admin_tds_report),
    path("api/v1/admin/commissions/export/", comm_views.admin_commissions_export),
    # Admin — Milestone bonuses
    path("api/v1/admin/milestones/", admin_ms_views.admin_milestones_dashboard),
    path("api/v1/admin/milestones/queue/", admin_ms_views.admin_milestones_queue),
    path(
        "api/v1/admin/milestones/queue/<int:record_id>/process/",
        admin_ms_views.admin_milestones_queue_process_one,
    ),
    path(
        "api/v1/admin/milestones/queue/process/",
        admin_ms_views.admin_milestones_queue_process_bulk,
    ),
    # Wallet & payouts bundle
    path("api/v1/user/payouts/", wallet_views.user_payouts_bundle),
    path("api/v1/user/wallet/", wallet_views.wallet_me),
    path("api/v1/user/wallet/transactions/", wallet_views.wallet_transactions),
    path("api/v1/user/wallet/bands/", wallet_views.wallet_bands),
    path("api/v1/user/wallet/withdraw/", wallet_views.wallet_withdraw),
    path("api/v1/user/wallet/withdrawals/", wallet_views.wallet_withdrawals_history),
    path(
        "api/v1/user/wallet/withdrawals/export/",
        wallet_views.wallet_withdrawals_export,
    ),
    path("api/v1/admin/withdrawals/", wallet_views.admin_withdrawals),
    path("api/v1/admin/withdrawals/pending/", wallet_views.admin_withdrawals_pending),
    path("api/v1/admin/withdrawals/<int:pk>/approve/", wallet_views.admin_withdrawal_approve),
    path("api/v1/admin/withdrawals/<int:pk>/reject/", wallet_views.admin_withdrawal_reject),
    path(
        "api/v1/admin/withdrawals/<int:pk>/mark-paid/",
        wallet_views.admin_withdrawal_mark_paid,
    ),
    path("api/v1/admin/withdrawals/batch-process/", wallet_views.admin_withdrawals_batch),
    path("api/v1/admin/withdrawals/export/", wallet_views.admin_withdrawals_export),
    # Sponsor slots
    path("api/v1/user/sponsor-slots/", slot_views.my_slots),
    path("api/v1/user/sponsor-slots/bundle/", slot_views.bundle),
    path("api/v1/user/sponsor-slots/<str:code>/share/", slot_views.share_code),
    path("api/v1/sponsor-slots/validate/", slot_views.validate_public),
    path("api/v1/admin/sponsor-slots/flagged/", slot_views.admin_slots_flagged),
    path("api/v1/admin/sponsor-slots/issue/", slot_views.admin_issue_slot),
    path("api/v1/admin/sponsor-slots/audit-log/", slot_views.admin_audit_log),
    path("api/v1/admin/sponsor-slots/<str:code>/expire/", slot_views.admin_expire_code),
    path("api/v1/admin/sponsor-slots/<str:code>/flag/", slot_views.admin_flag_code),
    path("api/v1/admin/sponsor-slots/<str:code>/clear-flag/", slot_views.admin_clear_flag_code),
    path("api/v1/admin/sponsor-slots/<str:code>/", slot_views.admin_slot_detail),
    path("api/v1/admin/sponsor-slots/", slot_views.admin_slots),
    # Courses
    path("api/v1/courses/", course_views.list_ebooks),
    path("api/v1/courses/bestsellers/", course_views.bestsellers),
    path("api/v1/courses/<int:pk>/", course_views.ebook_detail_by_id),
    path("api/v1/courses/<slug:slug>/", course_views.ebook_detail),
    path("api/v1/user/courses/enrolled/", course_views.my_enrollments),
    path("api/v1/user/courses/enrolled/<int:pk>/", course_views.my_enrolled_ebook_detail_by_id),
    path("api/v1/user/courses/enrolled/<slug:slug>/", course_views.my_enrolled_ebook_detail),
    path("api/v1/user/courses/<slug:slug>/download/", course_views.download_signed),
    path("api/v1/admin/courses/", course_views.admin_course_list),
    path("api/v1/admin/courses/<int:pk>/", course_views.admin_course_detail),
    path("api/v1/admin/courses/enrollments/", course_views.admin_enrollments),
    # Banners (public + admin)
    path("api/v1/banners/", banner_views.public_banners),
    path("api/v1/admin/banners/", banner_views.admin_banners),
    path("api/v1/admin/banners/<int:pk>/", banner_views.admin_banner_detail),
    # Payments
    path("api/v1/user/cart/", cart_views.cart_root),
    path("api/v1/user/cart/items/", cart_views.cart_add_item),
    path("api/v1/user/cart/items/<int:item_id>/", cart_views.cart_remove_item),
    path("api/v1/user/cart/checkout/", cart_views.cart_checkout),
    path("api/v1/payments/create-order/", pay_views.create_order),
    path("api/v1/payments/verify/", pay_views.verify),
    path("api/v1/payments/webhook/", pay_views.webhook),
    path("api/v1/user/orders/", pay_views.my_orders),
    path("api/v1/user/orders/<int:pk>/invoice/", pay_views.order_invoice),
    path("api/v1/user/orders/<int:pk>/refund/", pay_views.order_refund),
    path("api/v1/admin/orders/", pay_views.admin_orders),
    path(
        "api/v1/admin/orders/<str:order_ref>/verify-payment-manual/",
        pay_views.admin_verify_payment_manual,
    ),
    path("api/v1/admin/revenue/", pay_views.admin_revenue),
    path("api/v1/admin/gst-report/", pay_views.admin_gst_report),
    # Admin panel
    path("api/v1/admin/dashboard/", adminv.dashboard),
    path("api/v1/admin/users/", adminv.admin_users_list),
    path("api/v1/admin/users/<int:pk>/", adminv.admin_users_detail),
    path("api/v1/admin/users/<int:pk>/suspend/", adminv.admin_user_suspend),
    path("api/v1/admin/users/<int:pk>/unsuspend/", adminv.admin_user_unsuspend),
    path("api/v1/admin/users/kyc-queue/", adminv.kyc_queue),
    path("api/v1/admin/compliance-queue/", adminv.compliance_queue),
    path("api/v1/admin/users/<int:pk>/kyc/verify/", adminv.kyc_verify),
    path(
        "api/v1/admin/users/<int:pk>/compliance/approve/",
        adminv.compliance_approve,
    ),
    path(
        "api/v1/admin/users/compliance/approve/",
        adminv.compliance_approve,
    ),
    path(
        "api/v1/admin/users/<int:pk>/compliance/reject/",
        adminv.compliance_reject,
    ),
    path("api/v1/admin/agreements/", agreement_views.admin_legal_documents),
    path(
        "api/v1/admin/agreements/<int:pk>/",
        agreement_views.admin_legal_document_detail,
    ),
    path("api/v1/admin/users/delisted/", adminv.users_delisted),
    path("api/v1/admin/config/", adminv.system_config_view),
    path("api/v1/admin/reports/tds/", adminv.report_tds),
    path("api/v1/admin/reports/gst/", adminv.report_gst),
    path("api/v1/admin/reports/retail-ratio/", adminv.report_retail_ratio),
    path("api/v1/admin/reports/compliance/", adminv.report_compliance),
    path("api/v1/admin/grievances/", adminv.grievances_list),
    path("api/v1/admin/grievances/<int:pk>/respond/", adminv.grievance_respond),
]
if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif getattr(settings, "SERVE_MEDIA", False):
    media_prefix = settings.MEDIA_URL.lstrip("/").rstrip("/") + "/"
    urlpatterns += [
        re_path(
            rf"^{media_prefix}(?P<path>.*)$",
            serve,
            {"document_root": settings.MEDIA_ROOT},
        )
    ]
