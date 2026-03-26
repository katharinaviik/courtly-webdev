# backend/wallet/views.py
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.encoding import smart_str
import csv
import structlog

from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser

from .models import CoinLedger, TopupRequest
from .serializers import (
    WalletBalanceSerializer,
    CoinLedgerSerializer,
    TopupRequestCreateSerializer,
    TopupRequestListSerializer,
)
from .permissions import IsManager


logger = structlog.get_logger(__name__)


class WalletBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Return REAL wallet balance using CoinLedger as the source of truth.
        (Wallet.balance is ignored to prevent mismatch issues)
        """
        ledger_sum = (
            CoinLedger.objects.filter(user=request.user)
            .aggregate(s=Sum("amount"))
            .get("s") or 0
        )

        actor_name = request.user.get_full_name().strip() or request.user.username
        request_logger = logger.bind(
            event_name="wallet.balance.read",
            user_id=request.user.id,
            username=request.user.username,
            name=actor_name,
            role=getattr(request.user, "role", None),
            method=request.method,
            path=request.path,
        )

        request_logger.info(
            "wallet_balance_requested",
            outcome="success",
            balance_coins=int(ledger_sum),
        )

        return Response({"balance": int(ledger_sum)})


class CoinLedgerViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = CoinLedgerSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = CoinLedger.objects.select_related("ref_booking").order_by("-created_at")
        if IsManager().has_permission(self.request, self):
            user_id = self.request.query_params.get("user")
            return qs.filter(user_id=user_id) if user_id else qs
        return qs.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        logger.info(
            "coin_ledger_listed",
            event_name="wallet.ledger.list",
            user_id=request.user.id,
            username=request.user.username,
            role=getattr(request.user, "role", None),
            target_user=request.query_params.get("user"),
            result_count=len(response.data) if isinstance(response.data, list) else None,
            method=request.method,
            path=request.path,
            outcome="success",
        )
        return response

    @action(detail=False, methods=["get"], url_path="export-csv")
    def export_csv(self, request):
        qs = self.get_queryset().select_related("user")
        row_count = qs.count()
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = 'attachment; filename="wallet_transactions.csv"'
        resp.write("\ufeff")
        writer = csv.writer(resp)
        writer.writerow(["Reference ID", "Username", "Amount", "Date", "Type", "Status"])
        for row in qs:
            writer.writerow([
                row.id,
                smart_str(getattr(row.user, "username", "")),
                row.amount,
                row.created_at.strftime("%d %b %Y, %H:%M"),
                row.type.capitalize(),
                "Approved",
            ])
        logger.info(
            "coin_ledger_exported_csv",
            event_name="wallet.ledger.export_csv",
            user_id=request.user.id,
            username=request.user.username,
            role=getattr(request.user, "role", None),
            row_count=row_count,
            method=request.method,
            path=request.path,
            outcome="success",
        )
        return resp


class TopupRequestViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        qs = TopupRequest.objects.select_related("user").order_by("-created_at")
        if IsManager().has_permission(self.request, self):
            user_id = self.request.query_params.get("user")
            status_q = self.request.query_params.get("status")
            if user_id:
                qs = qs.filter(user_id=user_id)
            if status_q in {"pending", "approved", "rejected"}:
                qs = qs.filter(status=status_q)
            return qs
        return qs.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return TopupRequestCreateSerializer
        return TopupRequestListSerializer

    def get_serializer_context(self):
        """Ensure request is passed to serializer for building URLs."""
        context = super().get_serializer_context()
        context.update({"request": self.request})
        return context

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        logger.info(
            "topup_requests_listed",
            event_name="wallet.topup.list",
            user_id=request.user.id,
            username=request.user.username,
            role=getattr(request.user, "role", None),
            status_filter=request.query_params.get("status"),
            target_user=request.query_params.get("user"),
            result_count=len(response.data) if isinstance(response.data, list) else None,
            method=request.method,
            path=request.path,
            outcome="success",
        )
        return response

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        topup_id = response.data.get("id") if isinstance(response.data, dict) else None
        amount_thb = response.data.get("amount_thb") if isinstance(response.data, dict) else None
        logger.info(
            "topup_request_created",
            event_name="wallet.topup.create",
            user_id=request.user.id,
            username=request.user.username,
            role=getattr(request.user, "role", None),
            topup_id=topup_id,
            amount_thb=amount_thb,
            method=request.method,
            path=request.path,
            outcome="success",
        )
        return response

    @transaction.atomic
    @action(detail=True, methods=["post"], permission_classes=[IsManager])
    def approve(self, request, pk=None):
        topup = (
            TopupRequest.objects.select_for_update()
            .select_related("user")
            .filter(pk=pk)
            .first()
        )
        topup = get_object_or_404(TopupRequest, pk=pk) if topup is None else topup

        if topup.status != "pending":
            logger.warning(
                "topup_approve_invalid_state",
                event_name="wallet.topup.approve",
                actor_user_id=request.user.id,
                topup_id=topup.id,
                current_status=topup.status,
                outcome="invalid_state",
            )
            return Response({"detail": "This request was already processed."},
                            status=status.HTTP_400_BAD_REQUEST)

        # 1) Update topup status
        topup.status = "approved"
        topup.save(update_fields=["status"])

        # 2) Create ledger entry
        ledger = CoinLedger.objects.create(
            user=topup.user,
            type="topup",
            amount=topup.coins,
            ref_booking=None
        )

        # 3) Sync Wallet.balance to match with ledger
        from django.db.models import Sum
        from .models import Wallet

        ledger_sum = (
                CoinLedger.objects.filter(user=topup.user)
                .aggregate(s=Sum("amount"))
                .get("s") or 0
        )

        wallet, _ = Wallet.objects.get_or_create(user=topup.user)
        wallet.balance = ledger_sum
        wallet.save(update_fields=["balance"])

        # 4) Return response
        data = TopupRequestListSerializer(topup, context={"request": request}).data
        data["ledger_id"] = ledger.id
        logger.info(
            "topup_approved",
            event_name="wallet.topup.approve",
            actor_user_id=request.user.id,
            actor_username=request.user.username,
            topup_id=topup.id,
            target_user_id=topup.user_id,
            amount_thb=topup.amount_thb,
            credited_coins=topup.coins,
            ledger_id=ledger.id,
            outcome="success",
        )
        return Response(data, status=status.HTTP_200_OK)

    @transaction.atomic
    @action(detail=True, methods=["post"], permission_classes=[IsManager])
    def reject(self, request, pk=None):
        topup = TopupRequest.objects.select_for_update().filter(pk=pk).first()
        topup = get_object_or_404(TopupRequest, pk=pk) if topup is None else topup
        if topup.status != "pending":
            logger.warning(
                "topup_reject_invalid_state",
                event_name="wallet.topup.reject",
                actor_user_id=request.user.id,
                topup_id=topup.id,
                current_status=topup.status,
                outcome="invalid_state",
            )
            return Response({"detail": "This request was already processed."},
                            status=status.HTTP_400_BAD_REQUEST)

        topup.status = "rejected"
        topup.save(update_fields=["status"])
        logger.info(
            "topup_rejected",
            event_name="wallet.topup.reject",
            actor_user_id=request.user.id,
            actor_username=request.user.username,
            topup_id=topup.id,
            target_user_id=topup.user_id,
            amount_thb=topup.amount_thb,
            outcome="success",
        )
        return Response(TopupRequestListSerializer(topup, context={"request": request}).data,
                        status=status.HTTP_200_OK)
