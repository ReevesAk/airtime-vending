import logging

from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .ussd import handle_ussd_request

logger = logging.getLogger("vending")


class PlainTextRenderer(BaseRenderer):
    """The gateway contract requires a bare `CON `/`END ` text body, not a
    JSON envelope -- DRF's content negotiation is bypassed for the body
    itself, just used for parsing the inbound form post.
    """

    media_type = "text/plain"
    format = "txt"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data.encode("utf-8") if isinstance(data, str) else data


class UssdView(APIView):
    parser_classes = [FormParser, MultiPartParser]
    renderer_classes = [PlainTextRenderer]
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        session_id = request.data.get("sessionId")
        phone_number = request.data.get("phoneNumber")
        text = request.data.get("text", "")

        if not session_id or not phone_number:
            return Response("sessionId and phoneNumber are required", status=400)

        logger.info("ussd.callback session_id=%s phone=%s text=%r", session_id, phone_number, text)

        response_text = handle_ussd_request(session_id=session_id, agent_phone_number=phone_number, text=text)
        return Response(response_text)


ussd_view = UssdView.as_view()
