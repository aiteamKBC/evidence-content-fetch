from django.core.management.base import BaseCommand, CommandError

from aptem.services.aptem_auth import AptemLoginError, refresh_aptem_cookie


class Command(BaseCommand):
    help = "Log in to Aptem and refresh APTEM_COOKIE_HEADER in backend/.env."

    def handle(self, *args, **options):
        try:
            result = refresh_aptem_cookie()
        except AptemLoginError as exc:
            raise CommandError(str(exc)) from exc

        expires = result.expires_at.isoformat() if result.expires_at else "unknown"
        self.stdout.write(
            self.style.SUCCESS(
                f"Aptem cookie refreshed. AUTHTOKEN present: {result.auth_token_present}. Expires: {expires}"
            )
        )
