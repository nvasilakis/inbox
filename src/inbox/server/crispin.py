""" IMAPClient wrapper for Inbox.

Unfortunately, due IMAP's statefulness, to implement connection pooling we
have to shunt off dealing with the connection pool to the caller or we'll end
up trying to execute calls with the wrong folder selected some amount of the
time. That's why functions take a connection argument.
"""
import os
import time

from .log import get_logger
from .pool import get_connection_pool

from ..util.misc import or_none
from ..util.cache import get_cache, set_cache

__all__ = ['CrispinClient', 'DummyCrispinClient']

### decorators

def timed(fn):
    """ A decorator for timing methods. """
    def timed_fn(self, *args, **kwargs):
        start_time = time.time()
        ret = fn(self, *args, **kwargs)
        self.log.info("\t\tTook {0} seconds".format(str(time.time() - start_time)))
        return ret
    return timed_fn

### main stuff

def new_crispin(account_id, provider, dummy=False):
    crispin_module_for = dict(Gmail=GmailCrispinClient, IMAP=CrispinClient)

    cls = DummyCrispinClient if dummy else crispin_module_for[provider]
    return cls(account_id)

class CrispinClientBase(object):
    """
    One thing to note about crispin clients is that *all* calls operate on
    the currently selected folder.

    Crispin will NEVER implicitly select a folder for you.

    This is very important! IMAP only guarantees that folder message UIDs
    are valid for a "session", which is defined as from the time you
    SELECT a folder until the connection is closed or another folder is
    selected.
    """
    def __init__(self, account_id, cache=False):
        self.log = get_logger(account_id)
        self.account_id = account_id
        # IMAP isn't stateless :(
        self.selected_folder = None
        self._folder_names = None
        self.cache = cache

    def set_cache(self, data, *keys):
        key = os.path.join('account.{0}'.format(self.account_id),
                *[str(key) for key in keys])
        return set_cache(key, data)

    def get_cache(self, *keys):
        return get_cache(
                os.path.join('account.{0}'.format(self.account_id), *keys))

    def sync_folders(self, c):
        raise NotImplementedError

    @property
    def selected_folder_name(self):
        return or_none(self.selected_folder, lambda f: f[0])

    @property
    def selected_folder_info(self):
        return or_none(self.selected_folder, lambda f: f[1])

    @property
    def selected_highestmodseq(self):
        return or_none(self.selected_folder_info,
                lambda i: i['HIGHESTMODSEQ'])

    @property
    def selected_uidvalidity(self):
        return or_none(self.selected_folder_info,
                lambda i: long(i['UIDVALIDITY']))

    def select_folder(self, folder, uidvalidity_callback, c):
        """ Selects a given folder and makes sure to set the 'selected_folder'
            attribute to a (folder_name, select_info) pair.

            Selecting a folder indicates the start of an IMAP session.
            IMAP UIDs are only guaranteed valid for sessions, so the caller
            must provide a callback that checks UID validity.
        """
        select_info = self._do_select_folder(folder, c)
        self.selected_folder = (folder, select_info)
        # don't propagate cached information from previous session
        self._folder_names = None
        self.log.info('Selected folder {0} with {1} messages.'.format(
            folder, select_info['EXISTS']))
        return uidvalidity_callback(folder, select_info)

    def _do_select_folder(self, folder, c):
        raise NotImplementedError

    def folder_status(self, folder, c):
        return self._fetch_folder_status(folder, c)

    def _fetch_folder_status(self, folder, c):
        raise NotImplementedError

    def all_uids(self, c):
        """ Get all UIDs associated with the currently selected folder as
            a list of integers sorted in ascending order.
        """
        data = self._fetch_all_uids(c)
        return sorted([long(s) for s in data])

    def _fetch_all_uids(self, c):
        raise NotImplementedError

    def new_and_updated_uids(self, modseq, c):
        return self._fetch_new_and_updated_uids(modseq, c)

    def _fetch_new_and_updated_uids(self, modseq, c):
        raise NotImplementedError

class DummyCrispinClient(CrispinClientBase):
    """ A crispin client that doesn't actually use IMAP at all. Instead, it
        retrieves cached data from disk and allows one to "replay" previously
        cached actions.

        This allows us to rapidly iterate and debug the message ingester
        while offline, without hitting any IMAP API.
    """
    def _do_select_folder(self, folder, c):
        cached_data = self.get_cache(folder, 'select_info')

        assert cached_data is not None, \
                'no select_info cached for account {0} {1}'.format(
                        self.account_id, folder)
        return cached_data

    def _fetch_folder_status(self, folder, c):
        cached_data = self.get_cache(folder, 'status')

        assert cached_data is not None, \
                'no folder status cached for account {0} {1}'.format(
                        self.account_id, folder)
        return cached_data

    def _fetch_all_uids(self, c):
        cached_data = self.get_cache(self.selected_folder_name, 'all_uids')

        assert cached_data is not None, \
                'no all_uids cached for account {0} {1}'.format(
                        self.account_id, self.selected_folder_name)
        return cached_data

    def _fetch_g_metadata(self, uids, c):
        cached_data = self.get_cache(self.selected_folder_name, 'g_metadata')

        assert cached_data is not None, \
                'no g_metadata cached for account {0} {1}'.format(
                        self.account_id, self.selected_folder_name)
        return cached_data

    def _fetch_new_and_updated_uids(self, modseq, c):
        cached_data = self.get_cache(self.selected_folder_name, 'updated', modseq)

        assert cached_data is not None, \
                'no modseq uids cached for account {0} {1} modseq {2}'.format(
                        self.account_id, self.selected_folder_name, modseq)
        return cached_data

    def _fetch_flags(self, uids, c):
        # return { uid: data, uid: data }
        cached_data = dict()
        for uid in uids:
            cached_data[uid] = self.get_cache(
                    self.selected_folder_name,
                    self.selected_uidvalidity,
                    self.selected_highestmodseq,
                    uid, 'flags')

        assert cached_data, \
                'no flags cached for account {0} {1} uids {2}'.format(
                        self.account_id, self.selected_folder_name, uids)

        return cached_data

    def _fetch_uids(self, uids, c):
        # return { uid: data, uid: data }
        cached_data = dict()
        for uid in uids:
            cached_data[uid] = self.get_cache(
                    self.selected_folder_name,
                    self.selected_uidvalidity,
                    self.selected_highestmodseq,
                    uid, 'body')

        assert cached_data, \
                'no body cached for account {0} {1} uids {2}'.format(
                        self.account_id, self.selected_folder_name, uids)

        return cached_data

    def _fetch_folder_list(self, c):
        cached_data = self.get_cache('folders')

        assert cached_data is not None, \
                'no folder list cached for account {0}'.format(self.account_id)

        return cached_data

class CrispinClient(CrispinClientBase):
    """ Methods must be called using a connection from the pool, e.g.

        @retry
        def poll():
            with instance.pool.get() as c:
                instance.all_uids(c)

        We don't save c on instances to save messiness with garbage
        collection of connections.

        Pool connections have to be managed by the crispin caller because
        of IMAP's stateful sessions.
    """
    # how many messages to download at a time
    CHUNK_SIZE = 1

    def __init__(self, account_id, cache=False):
        self.pool = get_connection_pool(account_id)
        CrispinClientBase.__init__(self, account_id, cache)

    @timed
    def _do_select_folder(self, folder, c):
        # XXX: Remove readonly before implementing mutate commands!
        select_info = c.select_folder(folder, readonly=True)

        if self.cache:
            self.set_cache(select_info, folder, 'select_info')

        return select_info

    def _fetch_folder_status(self, folder, c):
        status = c.folder_status(folder,
                ('UIDVALIDITY', 'HIGHESTMODSEQ'))

        if self.cache:
            self.set_cache(status, folder, 'status')

        return status

    def _fetch_all_uids(self, c):
        data = c.search(['NOT DELETED'])

        if self.cache:
            self.set_cache(data, self.selected_folder_name, 'all_uids')

        return data

    @timed
    def _fetch_new_and_updated_uids(self, modseq, c):
        data = c.search(['NOT DELETED', "MODSEQ {0}".format(modseq)])

        if self.cache:
            self.set_cache(data, self.selected_folder_name, 'updated', modseq)

        return data

    def _fetch_flags(self, uids, c):
        raise NotImplementedError

    def _fetch_uids(self, uids, c):
        raise NotImplementedError

    def _fetch_folder_list(self, c):
        """ NOTE: XLIST is deprecated, so we just use LIST.

            An example response with some other flags:

              * LIST (\HasNoChildren) "/" "INBOX"
              * LIST (\Noselect \HasChildren) "/" "[Gmail]"
              * LIST (\HasNoChildren \All) "/" "[Gmail]/All Mail"
              * LIST (\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"
              * LIST (\HasNoChildren \Important) "/" "[Gmail]/Important"
              * LIST (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
              * LIST (\HasNoChildren \Junk) "/" "[Gmail]/Spam"
              * LIST (\HasNoChildren \Flagged) "/" "[Gmail]/Starred"
              * LIST (\HasNoChildren \Trash) "/" "[Gmail]/Trash"

            IMAPClient parses this response into a list of
            (flags, delimiter, name) tuples.
        """
        folders = c.list_folders()

        if self.cache:
            self.set_cache(folders, 'folders')

        return folders

class GmailCrispinClient(CrispinClient):
    def __init__(self, account_id, cache=False):
        CrispinClient.__init__(self, account_id, cache=False)

    def sync_folders(self, c):
        """ In Gmail, every message is a subset of All Mail, so we only sync
            that folder + Inbox (for quickly downloading initial inbox
            messages and continuing to receive new Inbox messages while a
            large mail archive is downloading).
        """
        return [self.folder_names(c)['Inbox'], self.folder_names(c)['All']]

    def flags(self, uids, c):
        """ Flags includes labels on Gmail because Gmail doesn't use \\Draft."""
        return dict([(uid, dict(flags=msg['FLAGS'], labels=msg['X-GM-LABELS']))
            for uid, msg in self._fetch_flags(uids, c).iteritems()])

    def _fetch_flags(self, uids, c):
        data = c.fetch(uids, ['FLAGS X-GM-LABELS'])

        if self.cache:
            # account.{{account_id}}/{{folder}}/{{uidvalidity}}/{{highestmodseq}}/{{uid}}/flags
            for uid in uids:
                self.set_cache(data[uid],
                        self.selected_folder_name,
                        self.selected_uidvalidity,
                        self.selected_highestmodseq,
                        uid, 'flags')

        return data

    def folder_names(self, c):
        """ Parses out Gmail-specific folder names based on Gmail IMAP flags.

            If the user's account is localized to a different language, it will
            return the proper localized string.

            Caches the call since we use it all over the place and folders
            never change names during a session.
        """
        if self._folder_names is None:
            folders = self._fetch_folder_list(c)
            self._folder_names = dict()
            for flags, delimiter, name in folders:
                is_label = True
                for flag in [u'\\All', '\\Drafts', '\\Important', '\\Sent',
                        '\\Junk', '\\Flagged', '\\Trash']:
                    # find localized names for Gmail's special folders
                    if flag in flags:
                        is_label = False
                        # strip off leading \ on flag
                        k = flag.replace('\\', '').capitalize()
                        self._folder_names[k] = name
                if name.capitalize() == 'Inbox':
                    is_label = False
                    self._folder_names[name.capitalize()] = name
                if u'\\Noselect' in flags:
                    # special folders that can't contain messages, usually
                    # just '[Gmail]'
                    is_label = False
                # everything else is a label
                if is_label:
                    self._folder_names.setdefault('Labels', list()).append(name)
            if 'Labels' in self._folder_names:
                self._folder_names['Labels'].sort()
        return self._folder_names

    def uids(self, uids, c):
        raw_messages = self._fetch_uids(uids, c)
        messages = []
        for uid in sorted(raw_messages.iterkeys(), key=int):
            msg = raw_messages[uid]
            messages.append((int(uid), msg['INTERNALDATE'], msg['FLAGS'],
                msg['BODY[]'], msg['X-GM-THRID'], msg['X-GM-MSGID'],
                msg['X-GM-LABELS']))
        return messages

    def _fetch_uids(self, uids, c):
        data = c.fetch(uids,
                ['BODY.PEEK[] INTERNALDATE FLAGS', 'X-GM-THRID',
                 'X-GM-MSGID', 'X-GM-LABELS'])
        for uid, msg in data.iteritems():
            # NOTE: flanker needs encoded bytestrings as its input, since to
            # deal properly with MIME-encoded email you need to do part
            # decoding based on message / MIME part headers anyway. imapclient
            # tries to abstract away bytes and decodes all bytes received from
            # the wire as _latin-1_, which is wrong in any case where 8bit MIME
            # is used. so we have to reverse the damage before we proceed.
            #
            # We should REMOVE this XXX HACK XXX when we finish working with
            # Menno to fix this problem upstream.
            msg['BODY[]'] = msg['BODY[]'].encode('latin-1')

        if self.cache:
            # account.{{account_id}}/{{folder}}/{{uidvalidity}}/{{highestmodseq}}/{{uid}}/body
            for uid in uids:
                self.set_cache(data[uid],
                        self.selected_folder_name,
                        self.selected_uidvalidity,
                        self.selected_highestmodseq,
                        uid, 'body')

        return data

    @timed
    def g_metadata(self, uids, c):
        """ Download Gmail MSGIDs and THRIDS for the given messages, or all
            messages in the currently selected folder if no UIDs specified.

            NOTE: only UIDs are guaranteed to be unique to a folder, G-MSGID
            and G-THRID may not be.
        """
        self.log.info("Fetching X-GM-MSGID and X-GM-THRID mapping from server.")
        return dict([(long(uid), dict(msgid=str(ret['X-GM-MSGID']),
            thrid=str(ret['X-GM-THRID']))) \
                for uid, ret in self._fetch_g_metadata(uids, c).iteritems()])

    def _fetch_g_metadata(self, uids, c):
        data = c.fetch(uids, ['X-GM-MSGID', 'X-GM-THRID'])

        if self.cache:
            self.set_cache(data, self.selected_folder_name, 'g_metadata')

        return data

    def expand_threads(self, thread_ids, c):
        """ Find all message UIDs in a user's account that have X-GM-THRID in
            thread_ids.

            Message UIDs returned are All Mail UIDs; this method requires the
            All Mail folder to be selected.
        """
        assert self.selected_folder_name == self.folder_names(c)['All'], \
                "must select All Mail first ({0})".format(
                        self.selected_folder_name)
        # UIDs ascend over time; return in order most-recent first
        return sorted(self._expand_threads(thread_ids, c), reverse=True)

    def _expand_threads(self, thread_ids, c):
        # The boolean IMAP queries use prefix notation for query params.
        # imaplib automatically adds parens.
        criteria = ('OR ' * (len(thread_ids)-1)) + ' '.join(
                ['X-GM-THRID {0}'.format(thrid) for thrid in thread_ids])
        data = c.search(['NOT DELETED', criteria])

        # if self.cache:
        #     self.set_cache(data, self.selected_folder_name, 'foo')

        return data
