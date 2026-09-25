from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings


@override_settings(
    FRONTEND_URL="https://chatipt.example",
    SOCIALACCOUNT_PROVIDERS={
        "orcid": {
            "APP": {"client_id": "client-id", "secret": "client-secret", "key": ""},
            "SCOPE": ["openid", "/authenticate"],
        }
    },
)
class OrcidCallbackTests(TestCase):
    @staticmethod
    def _response(payload):
        response = Mock(status_code=200)
        response.json.return_value = payload
        return response

    @patch("api.views.requests.get")
    @patch("api.views.requests.post")
    def test_reuses_existing_user_when_placeholder_email_case_differs(self, post, get):
        user_model = get_user_model()
        existing_user = user_model.objects.create_user(
            username="0000-0001-5730-517X@orcid.org",
            email="0000-0001-5730-517x@orcid.org",
            orcid_id="0000-0001-5730-517X",
        )
        post.return_value = self._response({"access_token": "new-token"})
        get.side_effect = [
            self._response({"sub": "0000-0001-5730-517X"}),
            self._response({"person": {}}),
        ]

        response = self.client.get("/api/auth/orcid/callback/?code=test-code")

        self.assertRedirects(
            response,
            "https://chatipt.example",
            fetch_redirect_response=False,
        )
        self.assertEqual(user_model.objects.count(), 1)
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(existing_user.pk))
        existing_user.refresh_from_db()
        self.assertEqual(existing_user.orcid_access_token, "new-token")

    @patch("api.views.requests.get")
    @patch("api.views.requests.post")
    def test_login_keeps_name_saved_on_account(self, post, get):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            username="0000-0001-5730-517X@orcid.org",
            email="0000-0001-5730-517X@orcid.org",
            orcid_id="0000-0001-5730-517X",
            first_name="Rukaya",
            last_name="Johaadien",
        )
        post.return_value = self._response({"access_token": "new-token"})
        get.side_effect = [
            self._response({"sub": "0000-0001-5730-517X"}),
            self._response({"person": {"name": {
                "given-names": {"value": "Different"},
                "family-name": {"value": "[unknown]"},
            }}}),
        ]

        response = self.client.get("/api/auth/orcid/callback/?code=test-code")

        self.assertRedirects(response, "https://chatipt.example", fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.last_name), ("Rukaya", "Johaadien"))
