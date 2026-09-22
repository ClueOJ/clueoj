from django.conf import settings

__all__ = ['last', 'post']

if not settings.EVENT_DAEMON_USE:
    real = False

    def post(channel, message):
        return 0

    def last():
        return 0
elif hasattr(settings, 'EVENT_DAEMON_AMQP'):
    from .event_poster_amqp import last, post
    real = True
else:
    from .event_poster_ws import last, post
    real = True


_transport_post = post


def post(channel, message):
    from judge.models import Submission
    submission_id = None
    if channel.startswith('sub_'):
        try:
            submission_id = int(channel[20:], 16)
        except ValueError:
            pass
    elif channel == 'submissions':
        submission_id = message.get('id')
    if submission_id and Submission.objects.filter(pk=submission_id, offline_hidden=True).exists():
        return 0
    return _transport_post(channel, message)
