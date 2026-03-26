from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from accounts.permissions import IsPlayer, IsManager
import structlog


logger = structlog.get_logger(__name__)


class PlayerHomeData(APIView):
    permission_classes = [IsAuthenticated, IsPlayer]

    def get(self, request):
        logger.info(
            "player_home_data_requested",
            event_name="core.player_home.read",
            user_id=request.user.id,
            username=request.user.username,
            role=getattr(request.user, "role", None),
            method=request.method,
            path=request.path,
            outcome="success",
        )
        return Response({"home": "This is player homepage data"})


class ManagerDashboardData(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        logger.info(
            "manager_dashboard_requested",
            event_name="core.manager_dashboard.read",
            user_id=request.user.id,
            username=request.user.username,
            role=getattr(request.user, "role", None),
            method=request.method,
            path=request.path,
            outcome="success",
        )
        return Response({"dashboard": "This is manager dashboard data"})
