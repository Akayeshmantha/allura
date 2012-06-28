'''Manage notifications and subscriptions

When an artifact is modified:
- Notification generated by tool app
- Search is made for subscriptions matching the notification
- Notification is added to each matching subscriptions' queue

Periodically:
- For each subscriptions with notifications and direct delivery:
   - For each notification, enqueue as a separate email message
   - Clear subscription's notification list
- For each subscription with notifications and delivery due:
   - Enqueue one email message with all notifications
   - Clear subscription's notification list

Notifications are also available for use in feeds
'''

import logging
from bson import ObjectId
from datetime import datetime, timedelta
from collections import defaultdict
from webhelpers import feedgenerator as FG

from pylons import c, g
from tg import config
import pymongo
import jinja2
import markdown

from ming import schema as S
from ming.orm import FieldProperty, ForeignIdProperty, RelationProperty, session
from ming.orm.declarative import MappedClass

from allura.lib.markdown_extensions import ForgeExtension
from allura.lib import helpers as h
from allura.lib import security
import allura.tasks.mail_tasks

from .session import main_orm_session, project_orm_session
from .auth import User


log = logging.getLogger(__name__)

MAILBOX_QUIESCENT=None # Re-enable with [#1384]: timedelta(minutes=10)

class Notification(MappedClass):
    class __mongometa__:
        session = main_orm_session
        name = 'notification'
        indexes = [ ('neighborhood_id', 'tool_name', 'pubdate'),
                    ('author_id',), # used in ext/user_profile/user_main.py for user feeds
                  ]

    _id = FieldProperty(str, if_missing=h.gen_message_id)

    # Classify notifications
    neighborhood_id = ForeignIdProperty('Neighborhood', if_missing=lambda:c.project.neighborhood._id)
    project_id = ForeignIdProperty('Project', if_missing=lambda:c.project._id)
    app_config_id = ForeignIdProperty('AppConfig', if_missing=lambda:c.app.config._id)
    tool_name = FieldProperty(str, if_missing=lambda:c.app.config.tool_name)
    ref_id = ForeignIdProperty('ArtifactReference')
    topic = FieldProperty(str)
    unique_id = FieldProperty(str, if_missing=lambda:h.nonce(40))

    # Notification Content
    in_reply_to=FieldProperty(str)
    from_address=FieldProperty(str)
    reply_to_address=FieldProperty(str)
    subject=FieldProperty(str)
    text=FieldProperty(str)
    link=FieldProperty(str)
    author_id=ForeignIdProperty('User')
    feed_meta=FieldProperty(S.Deprecated)
    artifact_reference = FieldProperty(S.Deprecated)
    pubdate = FieldProperty(datetime, if_missing=datetime.utcnow)

    ref = RelationProperty('ArtifactReference')

    view = jinja2.Environment(
            loader=jinja2.PackageLoader('allura', 'templates'))

    def author(self):
        return User.query.get(_id=self.author_id) or User.anonymous()

    @classmethod
    def post(cls, artifact, topic, **kw):
        '''Create a notification and  send the notify message'''
        import allura.tasks.notification_tasks
        n = cls._make_notification(artifact, topic, **kw)
        if n:
            allura.tasks.notification_tasks.notify.post(
                n._id, artifact.index_id(), topic)
        return n

    @classmethod
    def post_user(cls, user, artifact, topic, **kw):
        '''Create a notification and deliver directly to a user's flash
    mailbox'''
        try:
            mbox = Mailbox(user_id=user._id, is_flash=True,
                           project_id=None,
                           app_config_id=None)
            session(mbox).flush(mbox)
        except pymongo.errors.DuplicateKeyError:
            session(mbox).expunge(mbox)
            mbox = Mailbox.query.get(user_id=user._id, is_flash=True)
        n = cls._make_notification(artifact, topic, **kw)
        if n:
            mbox.queue.append(n._id)
        return n

    @classmethod
    def _make_notification(cls, artifact, topic, **kwargs):
        from allura.model import Project
        idx = artifact.index()
        subject_prefix = '[%s:%s] ' % (
            c.project.shortname, c.app.config.options.mount_point)
        if topic == 'message':
            post = kwargs.pop('post')
            text = post.text
            file_info = kwargs.pop('file_info', None)
            if file_info is not None:
                file_info.file.seek(0, 2)
                bytecount = file_info.file.tell()
                file_info.file.seek(0)
                text = "%s\n\n\nAttachment: %s (%s; %s)" % (text, file_info.filename, h.do_filesizeformat(bytecount), file_info.type)

            subject = post.subject or ''
            if post.parent_id and not subject.lower().startswith('re:'):
                subject = 'Re: ' + subject
            author = post.author()
            d = dict(
                _id=artifact.url()+post._id,
                from_address=str(author._id) if author != User.anonymous() else None,
                reply_to_address='"%s" <%s>' % (
                    subject_prefix, getattr(artifact, 'email_address', u'noreply@in.sf.net')),
                subject=subject_prefix + subject,
                text=text,
                in_reply_to=post.parent_id,
                author_id=author._id,
                pubdate=datetime.utcnow())
        else:
            subject = kwargs.pop('subject', '%s modified by %s' % (
                    idx['title_s'],c.user.get_pref('display_name')))
            reply_to = '"%s" <%s>' % (
                idx['title_s'],
                getattr(artifact, 'email_address', u'noreply@in.sf.net'))
            d = dict(
                from_address=reply_to,
                reply_to_address=reply_to,
                subject=subject_prefix + subject,
                text=kwargs.pop('text', subject),
                author_id=c.user._id,
                pubdate=datetime.utcnow())
            if c.user.get_pref('email_address'):
                d['from_address'] = '"%s" <%s>' % (
                    c.user.get_pref('display_name'),
                    c.user.get_pref('email_address'))
            elif c.user.email_addresses:
                d['from_address'] = '"%s" <%s>' % (
                    c.user.get_pref('display_name'),
                    c.user.email_addresses[0])
        if not d.get('text'):
            d['text'] = ''
        try:
            ''' Add addional text to the notification e-mail based on the artifact type '''
            template = cls.view.get_template('mail/' + artifact.type_s + '.txt')
            d['text'] += template.render(dict(c=c, g=g, config=config, data=artifact))
        except jinja2.TemplateNotFound:
            pass
        except:
            ''' Catch any errors loading or rendering the template,
            but the notification still gets sent if there is an error
            '''
            log.warn('Could not render notification template %s' % artifact.type_s, exc_info=True)

        assert d['reply_to_address'] is not None
        project = Project.query.get(_id=d.get('project_id', c.project._id))
        if project.notifications_disabled:
            log.info('Notifications disabled for project %s, not sending %s(%r)',
                     project.shortname, topic, artifact)
            return None
        n = cls(ref_id=artifact.index_id(),
                topic=topic,
                link=kwargs.pop('link', artifact.url()),
                **d)
        return n

    @classmethod
    def feed(cls, q, feed_type, title, link, description,
             since=None, until=None, offset=None, limit=None):
        """Produces webhelper.feedgenerator Feed"""
        d = dict(title=title, link=h.absurl(link), description=description, language=u'en')
        if feed_type == 'atom':
            feed = FG.Atom1Feed(**d)
        elif feed_type == 'rss':
            feed = FG.Rss201rev2Feed(**d)
        query = defaultdict(dict)
        query.update(q)
        if since is not None:
            query['pubdate']['$gte'] = since
        if until is not None:
            query['pubdate']['$lte'] = until
        cur = cls.query.find(query)
        cur = cur.sort('pubdate', pymongo.DESCENDING)
        if limit is None: limit = 10
        query = cur.limit(limit)
        if offset is not None: query = cur.offset(offset)
        for r in cur:
            feed.add_item(title=r.subject,
                          link=h.absurl(r.link.encode('utf-8')),
                          pubdate=r.pubdate,
                          description=r.text,
                          unique_id=r.unique_id,
                          author_name=r.author().display_name,
                          author_link=h.absurl(r.author().url()))
        return feed

    def footer(self):
        template = self.view.get_template('mail/footer.txt')
        return template.render(dict(
            notification=self,
            prefix=config.get('forgemail.url', 'https://sourceforge.net')))

    def send_simple(self, toaddr):
        allura.tasks.mail_tasks.sendsimplemail.post(
            toaddr=toaddr,
            fromaddr=self.from_address,
            reply_to=self.reply_to_address,
            subject=self.subject,
            message_id=self._id,
            in_reply_to=self.in_reply_to,
            text=(self.text or '') + self.footer())

    def send_direct(self, user_id):
        user = User.query.get(_id=ObjectId(user_id))
        artifact = self.ref.artifact
        # Don't send if user doesn't have read perms to the artifact
        if user and artifact and \
                not security.has_access(artifact, 'read', user)():
            log.debug("Skipping notification - User %s doesn't have read "
                      "access to artifact %s" % (user_id, str(self.ref_id)))
            return
        allura.tasks.mail_tasks.sendmail.post(
            destinations=[str(user_id)],
            fromaddr=self.from_address,
            reply_to=self.reply_to_address,
            subject=self.subject,
            message_id=self._id,
            in_reply_to=self.in_reply_to,
            text=(self.text or '') + self.footer())

    @classmethod
    def send_digest(self, user_id, from_address, subject, notifications,
                    reply_to_address=None):
        if not notifications: return
        # Filter out notifications for which the user doesn't have read
        # permissions to the artifact.
        user = User.query.get(_id=ObjectId(user_id))
        artifact = self.ref.artifact
        def perm_check(notification):
            return not (user and artifact) or \
                    security.has_access(artifact, 'read', user)()
        notifications = filter(perm_check, notifications)

        if reply_to_address is None:
            reply_to_address = from_address
        text = [ 'Digest of %s' % subject ]
        for n in notifications:
            text.append('From: %s' % n.from_address)
            text.append('Subject: %s' % (n.subject or '(no subject)'))
            text.append('Message-ID: %s' % n._id)
            text.append('')
            text.append(n.text or '-no text-')
        text.append(n.footer())
        text = '\n'.join(text)
        allura.tasks.mail_tasks.sendmail.post(
            destinations=[str(user_id)],
            fromaddr=from_address,
            reply_to=reply_to_address,
            subject=subject,
            message_id=h.gen_message_id(),
            text=text)

    @classmethod
    def send_summary(self, user_id, from_address, subject, notifications):
        if not notifications: return
        text = [ 'Digest of %s' % subject ]
        for n in notifications:
            text.append('From: %s' % n.from_address)
            text.append('Subject: %s' % (n.subject or '(no subject)'))
            text.append('Message-ID: %s' % n._id)
            text.append('')
            text.append(h.text.truncate(n.text or '-no text-', 128))
        text.append(n.footer())
        text = '\n'.join(text)
        allura.tasks.mail_tasks.sendmail.post(
            destinations=[str(user_id)],
            fromaddr=from_address,
            reply_to=from_address,
            subject=subject,
            message_id=h.gen_message_id(),
            text=text)

class Mailbox(MappedClass):
    class __mongometa__:
        session = main_orm_session
        name = 'mailbox'
        unique_indexes = [
            ('user_id', 'project_id', 'app_config_id',
             'artifact_index_id', 'topic', 'is_flash'),
            ]
        indexes = [
            ('project_id', 'artifact_index_id'),
            ('is_flash', 'user_id'),
            ('type', 'next_scheduled')]

    _id = FieldProperty(S.ObjectId)
    user_id = ForeignIdProperty('User', if_missing=lambda:c.user._id)
    project_id = ForeignIdProperty('Project', if_missing=lambda:c.project._id)
    app_config_id = ForeignIdProperty('AppConfig', if_missing=lambda:c.app.config._id)

    # Subscription filters
    artifact_title = FieldProperty(str)
    artifact_url = FieldProperty(str)
    artifact_index_id = FieldProperty(str)
    topic = FieldProperty(str)

    # Subscription type
    is_flash = FieldProperty(bool, if_missing=False)
    type = FieldProperty(S.OneOf('direct', 'digest', 'summary', 'flash'))
    frequency = FieldProperty(dict(
            n=int,unit=S.OneOf('day', 'week', 'month')))
    next_scheduled = FieldProperty(datetime, if_missing=datetime.utcnow)

    # Actual notification IDs
    last_modified = FieldProperty(datetime, if_missing=datetime(2000,1,1))
    queue = FieldProperty([str])

    project = RelationProperty('Project')
    app_config = RelationProperty('AppConfig')

    @classmethod
    def subscribe(
        cls,
        user_id=None, project_id=None, app_config_id=None,
        artifact=None, topic=None,
        type='direct', n=1, unit='day'):
        if user_id is None: user_id = c.user._id
        if project_id is None: project_id = c.project._id
        if app_config_id is None: app_config_id = c.app.config._id
        tool_already_subscribed = cls.query.get(user_id=user_id,
            project_id=project_id,
            app_config_id=app_config_id,
            artifact_index_id=None)
        if tool_already_subscribed:
            log.debug('Tried to subscribe to artifact %s, while there is a tool subscription', artifact)
            return
        if artifact is None:
            artifact_title = 'All artifacts'
            artifact_url = None
            artifact_index_id = None
        else:
            i = artifact.index()
            artifact_title = i['title_s']
            artifact_url = artifact.url()
            artifact_index_id = i['id']
            artifact_already_subscribed = cls.query.get(user_id=user_id,
                project_id=project_id,
                app_config_id=app_config_id,
                artifact_index_id=artifact_index_id)
            if artifact_already_subscribed:
                return
        d = dict(user_id=user_id, project_id=project_id, app_config_id=app_config_id,
                 artifact_index_id=artifact_index_id, topic=topic)
        sess = session(cls)
        try:
            mbox = cls(
                type=type, frequency=dict(n=n, unit=unit),
                artifact_title=artifact_title,
                artifact_url=artifact_url,
                **d)
            sess.flush(mbox)
        except pymongo.errors.DuplicateKeyError:
            sess.expunge(mbox)
            mbox = cls.query.get(**d)
            mbox.artifact_title = artifact_title
            mbox.artifact_url = artifact_url
            mbox.type = type
            mbox.frequency.n = n
            mbox.frequency.unit = unit
            sess.flush(mbox)
        if not artifact_index_id:
            # Unsubscribe from individual artifacts when subscribing to the tool
            for other_mbox in cls.query.find(dict(
                user_id=user_id, project_id=project_id, app_config_id=app_config_id)):
                if other_mbox is not mbox:
                    other_mbox.delete()

    @classmethod
    def unsubscribe(
        cls,
        user_id=None, project_id=None, app_config_id=None,
        artifact_index_id=None, topic=None):
        if user_id is None: user_id = c.user._id
        if project_id is None: project_id = c.project._id
        if app_config_id is None: app_config_id = c.app.config._id
        cls.query.remove(dict(
                user_id=user_id,
                project_id=project_id,
                app_config_id=app_config_id,
                artifact_index_id=artifact_index_id,
                topic=topic))

    @classmethod
    def subscribed(
        cls, user_id=None, project_id=None, app_config_id=None,
        artifact=None, topic=None):
        if user_id is None: user_id = c.user._id
        if project_id is None: project_id = c.project._id
        if app_config_id is None: app_config_id = c.app.config._id
        if artifact is None:
            artifact_index_id = None
        else:
            i = artifact.index()
            artifact_index_id = i['id']
        return cls.query.find(dict(
                user_id=user_id,
                project_id=project_id,
                app_config_id=app_config_id,
                artifact_index_id=artifact_index_id)).count() != 0

    @classmethod
    def deliver(cls, nid, artifact_index_id, topic):
        '''Called in the notification message handler to deliver notification IDs
        to the appropriate  mailboxes.  Atomically appends the nids
        to the appropriate mailboxes.
        '''
        d = {
            'project_id':c.project._id,
            'app_config_id':c.app.config._id,
            'artifact_index_id':{'$in':[None, artifact_index_id]},
            'topic':{'$in':[None, topic]}
            }
        for mbox in cls.query.find(d):
            mbox.query.update(
                {'$push':dict(queue=nid),
                 '$set':dict(last_modified=datetime.utcnow())})
            # Make sure the mbox doesn't stick around to be flush()ed
            session(mbox).expunge(mbox)

    @classmethod
    def fire_ready(cls):
        '''Fires all direct subscriptions with notifications as well as
        all summary & digest subscriptions with notifications that are ready
        '''
        now = datetime.utcnow()
        # Queries to find all matching subscription objects
        q_direct = dict(
            type='direct',
            queue={'$ne':[]})
        if MAILBOX_QUIESCENT:
            q_direct['last_modified']={'$lt':now - MAILBOX_QUIESCENT}
        q_digest = dict(
            type={'$in': ['digest', 'summary']},
            next_scheduled={'$lt':now})
        for mbox in cls.query.find(q_direct):
            mbox = cls.query.find_and_modify(
                query=dict(_id=mbox._id),
                update={'$set': dict(
                        queue=[])},
                new=False)
            mbox.fire(now)
        for mbox in cls.query.find(q_digest):
            next_scheduled = now
            if mbox.frequency.unit == 'day':
                next_scheduled += timedelta(days=mbox.frequency.n)
            elif mbox.frequency.unit == 'week':
                next_scheduled += timedelta(days=7 * mbox.frequency.n)
            elif mbox.frequency.unit == 'month':
                next_scheduled += timedelta(days=30 * mbox.frequency.n)
            mbox = cls.query.find_and_modify(
                query=dict(_id=mbox._id),
                update={'$set': dict(
                        next_scheduled=next_scheduled,
                        queue=[])},
                new=False)
            mbox.fire(now)

    def fire(self, now):
        notifications = Notification.query.find(dict(_id={'$in':self.queue}))
        notifications = notifications.all()
        if self.type == 'direct':
            ngroups = defaultdict(list)
            for n in notifications:
                if n.topic == 'message':
                    n.send_direct(self.user_id)
                    # Messages must be sent individually so they can be replied
                    # to individually
                else:
                    key = (n.subject, n.from_address, n.reply_to_address, n.author_id)
                    ngroups[key].append(n)
            # Accumulate messages from same address with same subject
            for (subject, from_address, reply_to_address, author_id), ns in ngroups.iteritems():
                if len(ns) == 1:
                    n.send_direct(self.user_id)
                else:
                    Notification.send_digest(
                        self.user_id, from_address, subject, ns, reply_to_address)
        elif self.type == 'digest':
            Notification.send_digest(
                self.user_id, u'noreply@in.sf.net', 'Digest Email',
                notifications)
        elif self.type == 'summary':
            Notification.send_summary(
                self.user_id, u'noreply@in.sf.net', 'Digest Email',
                notifications)
