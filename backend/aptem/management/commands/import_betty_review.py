import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from aptem.services.review_capture import capture_review
from aptem.services.review_probe import run_probe
from aptem.services.review_storage import destination_learner, save_review
from aptem.services.learner_reviews import capture_all_finished


class Command(BaseCommand):
    help = 'Extract Betty Heaton first Finished review; save to Learner.reviews/review_sections unless --dry-run.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--all-finished', action='store_true')

    def handle(self, *args, **options):
        try:
            destination = destination_learner(15997)
            payloads = []
            def collect(*args):
                if options['all_finished']:
                    payloads.extend(capture_all_finished(*args, write=self.stdout.write))
                else:
                    payloads.append(capture_review(*args))
            run_probe(self.stdout.write, on_review=collect)
            if not payloads:
                raise ValueError('No completed reviews captured')
            path = settings.BASE_DIR.parent/'debug'/('betty-reviews.json' if options['all_finished'] else 'betty-review.json')
            path.write_text(json.dumps(payloads if options['all_finished'] else payloads[0], indent=2, ensure_ascii=False), encoding='utf-8')
            self.stdout.write(json.dumps({'result':'extracted','destination_learner_id':destination,
                                         'reviews':len(payloads),'sections':sum(len(p['sections']) for p in payloads),'dry_run':options['dry_run']}))
            if not options['dry_run']:
                for payload in payloads:
                    self.stdout.write(json.dumps({'result':'saved', 'aptem_review_id':payload['aptem_review_id'], **save_review(payload)}))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
