import itertools
import operator
from collections import defaultdict

import pymongo
from django.core import urlresolvers
from django.template.defaultfilters import slugify

from .base import (db, Document, RelatedDocuments, ListManager)
from .metadata import Metadata
from .utils import CachedAttribute


class Role(dict):

    def data(self):
        '''This roles term metadata from the metadata['terms'] list.
        '''
        metadata = self.manager.document
        return metadata.term_dict[self['term']]

    def is_committee(self):
        return ('committee' in self)

    def committee_name(self):
        name = self['committee']
        if 'subcommittee' in self:
            sub = self['subcommittee']
            if sub:
                name = '%s - %s' % (name, sub)
        return name

    def type_display(self):
        ignored_role_types = [
            'member',
            'committee member',
            ]
        if self['type'] in ignored_role_types:
            return ''
        else:
            return self['type'].title()


class RolesManager(ListManager):
    wrapper = Role


class OldRole(dict):
    '''The `OldRolesManager` has been taken out behind the barn and
    shot on account of its confusingness. To replace it, this wrapper
    class defines a few helpful methods for templates to use, and
    each Legislator object dynamically gets its own OldRole class
    created as a subclass of this class, with an extra attr `document`
    pointing back to the related legislator object. The method that
    creates the new OldRole class is Legislator._old_role_wrapper.
    '''

    @property
    def termdata(self):
        dict_ = self.document.metadata.terms_manager.dict_
        return dict_[self['term']]

    def chamber_name(self):
        chamber = self['chamber']
        if chamber == 'joint':
            return 'Joint'
        return self.document.metadata['%s_chamber_name' % self['chamber']]

    def committee_object(self):
        '''If the committee id no longer exists in mongo for some reason,
        this function returns None.
        '''
        if 'committee_id' in self:
            _id = self['committee_id']
            return self.document._old_roles_committees.get(_id)
        else:
            return self


class Legislator(Document):

    collection = db.legislators
    instance_key = 'leg_id'

    committees = RelatedDocuments('Committee', model_keys=['members.leg_id'])
    feed_entries = RelatedDocuments('FeedEntry', model_keys=['entity_ids'],
                                    sort=[('published_parsed', -1)])
    roles_manager = RolesManager()
    votes_manager = RelatedDocuments('BillVote', model_keys=['_voters'])

    @property
    def metadata(self):
        return Metadata.get_object(self['state'])

    def slug(self):
        return slugify(self.display_name())

    def get_absolute_url(self):
        kwargs = dict(abbr=self.metadata['abbreviation'],
                      _id=self.id,
                      slug=self.slug())
        return urlresolvers.reverse('legislator', kwargs=kwargs)

    def get_sponsored_bills_url(self):
        kwargs = dict(abbr=self.metadata['abbreviation'])
        return ''.join(urlresolvers.reverse('bills', kwargs=kwargs),
                       '?sponsor__leg_id=', self.id)

    def votes_5_sorted(self):
        _id = self['_id']
        votes = self.votes_manager(limit=5,
            sort=[('date', pymongo.DESCENDING)])
        for vote in votes:
            vote_value = 'other'
            for obj in vote['yes_votes']:
                if _id == obj['leg_id']:
                    vote_value = 'yes'
                    break
            if vote_value == 'other':
                for obj in vote['no_votes']:
                    if _id == obj['leg_id']:
                        vote_value = 'no'
                        break
            yield vote_value, vote

    def sponsored_bills(self, extra_spec=None, *args, **kwargs):
        if extra_spec is None:
            extra_spec = {}
        extra_spec.update({'sponsors.leg_id': self.id})
        if 'sort' not in kwargs:
            kwargs.update(sort=[('updated_at', pymongo.DESCENDING)])
        return self.metadata.bills(extra_spec, *args, **kwargs)

    def primary_sponsored_bills(self, fields=None):
        kwargs = {}
        if fields is not None:
            kwargs['fields'] = fields
        return self.metadata.bills({'sponsors.type': 'primary',
                                    'sponsors.leg_id': self.id}, **kwargs)

    def secondary_sponsored_bills(self):
        return self.metadata.bills({'sponsors.type': {'$ne': 'primary'},
                                    'sponsors.leg_id': self.id})

    def display_name(self):
        return self['full_name']

    def title(self):
        chamber = self.get('chamber')
        if chamber:
            return self.metadata[chamber + '_chamber_title']
        else:
            return ''

    def sessions_served(self):
        session_details = self.metadata['session_details']
        terms = self.metadata['terms']
        for role in self['roles']:
            if role['type'] == 'member':
                term_name = role['term']

                try:
                    details = session_details[term_name]
                except KeyError:
                    for term in terms:
                        if term['name'] == term_name:
                            for session in term['sessions']:
                                details = session_details[session]
                                yield details['display_name']
                else:
                    yield details['display_name']

    @CachedAttribute
    def vote_role(self):
        # If this is an active legislator, access party and district
        # directly on the legislator.
        if self['active']:
            return self

        # Else, get them from the old_roles dict, with the term
        # defined by the term of the bill this legislator was
        # ultimately retrieved in relation to.
        vote = self.vote
        bill = vote.bill()
        term = bill['_term']
        roles = self['old_roles'][term]
        chamber = vote['chamber']

        # ...and use the bill's chamber too.
        roles = filter(lambda role: role['chamber'] == chamber, roles)

        # ...and the specific date defined by the date of the vote.
        if len(roles) == 1:
            return roles.pop()
        else:
            vote_date = self.vote['date']
            for role in roles:
                start_date = role.get('start_date')
                end_date = role.get('end_date')
                if start_date and end_date:
                    if start_date < vote_date < end_date:
                        return role

    def old_sessions_served(self):
        '''Returns the sessions served info from
        old_roles.'''
        display_names = set()
        term_dict = self.metadata.term_dict
        session_details = self.metadata['session_details']
        for term, oldroles_list in self['old_roles'].items():
            for role in oldroles_list:
                termdata = term_dict[term]
                for term_ in termdata:
                    for session in term_['sessions']:
                        name = session_details[session]['display_name']
                        display_names.add(name)
        return display_names

    @CachedAttribute
    def _old_roles_committees(self):
        ids = []
        for term, roles in self['old_roles'].items():
            _ids = [role.get('committee_id') for role in roles]
            _ids = filter(None, _ids)
            ids.extend(_ids)
        objs = list(db.committees.find({'_id': {'$in': ids}}))
        objs = dict((objs['_id'], objs) for objs in objs)
        return objs

    @CachedAttribute
    def _old_role_wrapper(self):
        cls = type('OldRole', (OldRole,), dict(document=self))
        return cls

    def old_roles_manager(self):
        '''Return old roles, grouped first by term, then by chamber,
        then by type.'''
        wrapper = self._old_role_wrapper
        chamber_getter = operator.itemgetter('chamber')
        for term, roles in self['old_roles'].items():
            chamber_roles = defaultdict(lambda: defaultdict(list))
            for chamber, roles in itertools.groupby(roles, chamber_getter):
                for role in roles:
                    role = wrapper(role)
                    typeslug = role['type'].lower().replace(' ', '_')
                    chamber_roles[chamber][typeslug].append(role)
            yield term, chamber_roles

