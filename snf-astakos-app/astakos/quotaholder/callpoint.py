# Copyright 2012, 2013 GRNET S.A. All rights reserved.
#
# Redistribution and use in source and binary forms, with or
# without modification, are permitted provided that the following
# conditions are met:
#
#   1. Redistributions of source code must retain the above
#      copyright notice, this list of conditions and the following
#      disclaimer.
#
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY GRNET S.A. ``AS IS'' AND ANY EXPRESS
# OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL GRNET S.A OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
# AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and
# documentation are those of the authors and should not be
# interpreted as representing official policies, either expressed
# or implied, of GRNET S.A.

from django.db.models import F
from astakos.quotaholder.exception import (
    QuotaholderError,
    NoCommissionError,
    CorruptedError, InvalidDataError,
    NoHoldingError,
    DuplicateError)

from astakos.quotaholder.commission import (
    Import, Release, Operations, finalize, undo)

from astakos.quotaholder.utils.newname import newname
from astakos.quotaholder.api import QH_PRACTICALLY_INFINITE

from .models import (Holding,
                     Commission, Provision, ProvisionLog,
                     now)


class QuotaholderDjangoDBCallpoint(object):

    def get_holder_quota(self, holders=None, sources=None, resources=None):
        holdings = Holding.objects.filter(holder__in=holders)

        if sources is not None:
            holdings = holdings.filter(source__in=sources)

        if resources is not None:
            holdings = holdings.filter(resource__in=resources)

        quotas = {}
        for holding in holdings:
            key = (holding.holder, holding.source, holding.resource)
            value = (holding.limit, holding.imported_min, holding.imported_max)
            quotas[key] = value

        return quotas

    def _get_holdings_for_update(self, holding_keys):
        holding_keys = sorted(holding_keys)
        holdings = {}
        for (holder, source, resource) in holding_keys:
            try:
                h = Holding.objects.get_for_update(
                    holder=holder, source=source, resource=resource)
                holdings[(holder, source, resource)] = h
            except Holding.DoesNotExist:
                pass
        return holdings

    def _provisions_to_list(self, provisions):
        lst = []
        for provision in provisions:
            try:
                holder = provision['holder']
                source = provision['source']
                resource = provision['resource']
                quantity = provision['quantity']
                key = (holder, source, resource)
                lst.append((key, quantity))
            except KeyError:
                raise InvalidDataError("Malformed provision")
        return lst

    def _mkProvision(self, key, quantity):
        holder, source, resource = key
        return {'holder': holder,
                'source': source,
                'resource': resource,
                'quantity': quantity,
                }

    def set_holder_quota(self, quotas):
        q = self._level_quota_dict(quotas)
        self.set_quota(q)

    def _level_quota_dict(self, quotas):
        lst = []
        for holder, holder_quota in quotas.iteritems():
            for source, source_quota in holder_quota.iteritems():
                for resource, limit in source_quota.iteritems():
                    key = (holder, source, resource)
                    lst.append((key, limit))
        return lst

    def set_quota(self, quotas):
        holding_keys = [key for (key, limit) in quotas]
        holdings = self._get_holdings_for_update(holding_keys)

        for key, limit in quotas:
            try:
                h = holdings[key]
            except KeyError:
                holder, source, resource = key
                h = Holding(holder=holder,
                            source=source,
                            resource=resource)
            h.limit = limit
            h.save()
            holdings[key] = h

    def add_resource_limit(self, source, resource, diff):
        objs = Holding.objects.filter(source=source, resource=resource)
        objs.update(limit=F('limit')+diff)

    def issue_commission(self,
                         context=None,
                         clientkey=None,
                         name=None,
                         force=False,
                         provisions=()):

        if name is None:
            name = ""
        create = Commission.objects.create
        commission = create(clientkey=clientkey, name=name)
        serial = commission.serial

        operations = Operations()

        provisions = self._provisions_to_list(provisions)
        keys = [key for (key, value) in provisions]
        holdings = self._get_holdings_for_update(keys)
        try:
            checked = []
            for key, quantity in provisions:
                if not isinstance(quantity, (int, long)):
                    raise InvalidDataError("Malformed provision")

                if key in checked:
                    m = "Duplicate provision for %s" % str(key)
                    provision = self._mkProvision(key, quantity)
                    raise DuplicateError(m,
                                         provision=provision)
                checked.append(key)

                # Target
                try:
                    th = holdings[key]
                except KeyError:
                    m = ("There is no such holding %s" % key)
                    provision = self._mkProvision(key, quantity)
                    raise NoHoldingError(m,
                                         provision=provision)

                if quantity >= 0:
                    operations.prepare(Import, th, quantity, force)

                else: # release
                    abs_quantity = -quantity
                    operations.prepare(Release, th, abs_quantity, force)

                holdings[key] = th
                Provision.objects.create(serial=commission,
                                         holder=th.holder,
                                         source=th.source,
                                         resource=th.resource,
                                         quantity=quantity)

        except QuotaholderError:
            operations.revert()
            raise

        return serial

    def _log_provision(self,
                       commission, provision, holding, log_time, reason):

        kwargs = {
            'serial':              commission.serial,
            'name':                commission.name,
            'holder':              holding.holder,
            'source':              holding.source,
            'resource':            holding.resource,
            'limit':               holding.limit,
            'imported_min':        holding.imported_min,
            'imported_max':        holding.imported_max,
            'delta_quantity':      provision.quantity,
            'issue_time':          commission.issue_time,
            'log_time':            log_time,
            'reason':              reason,
        }

        ProvisionLog.objects.create(**kwargs)

    def _get_commissions_for_update(self, clientkey, serials):
        cs = Commission.objects.filter(
            clientkey=clientkey, serial__in=serials).select_for_update()

        commissions = {}
        for c in cs:
            commissions[c.serial] = c
        return commissions

    def _partition_by(self, f, l):
        d = {}
        for x in l:
            group = f(x)
            group_l = d.get(group, [])
            group_l.append(x)
            d[group] = group_l
        return d

    def resolve_pending_commissions(self,
                                    context=None, clientkey=None,
                                    accept_set=[], reject_set=[],
                                    reason=''):
        actions = dict.fromkeys(accept_set, True)
        conflicting = set()
        for serial in reject_set:
            if actions.get(serial) is True:
                actions.pop(serial)
                conflicting.add(serial)
            else:
                actions[serial] = False

        conflicting = list(conflicting)
        serials = actions.keys()
        commissions = self._get_commissions_for_update(clientkey, serials)
        ps = Provision.objects.filter(serial__in=serials).select_for_update()
        holding_keys = sorted(p.holding_key() for p in ps)
        holdings = self._get_holdings_for_update(holding_keys)
        provisions = self._partition_by(lambda p: p.serial_id, ps)

        log_time = now()

        accepted, rejected, notFound = [], [], []
        for serial, accept in actions.iteritems():
            commission = commissions.get(serial)
            if commission is None:
                notFound.append(serial)
                continue

            accepted.append(serial) if accept else rejected.append(serial)

            ps = provisions.get(serial)
            assert ps is not None
            for pv in ps:
                key = pv.holding_key()
                h = holdings.get(key)
                if h is None:
                    raise CorruptedError("Corrupted provision")

                quantity = pv.quantity
                action = finalize if accept else undo
                if quantity >= 0:
                    action(Import, h, quantity)
                else:  # release
                    action(Release, h, -quantity)

                prefix = 'ACCEPT:' if accept else 'REJECT:'
                comm_reason = prefix + reason[-121:]
                self._log_provision(commission, pv, h, log_time, comm_reason)
                pv.delete()
            commission.delete()
        return accepted, rejected, notFound, conflicting

    def resolve_pending_commission(self, clientkey, serial, accept=True):
        if accept:
            ok, notOk, notF, confl = self.resolve_pending_commissions(
                clientkey=clientkey, accept_set=[serial])
        else:
            notOk, ok, notF, confl = self.resolve_pending_commissions(
                clientkey=clientkey, reject_set=[serial])

        assert notOk == confl == []
        assert ok + notF == [serial]
        return bool(ok)

    def get_pending_commissions(self, context=None, clientkey=None):
        pending = Commission.objects.filter(clientkey=clientkey)
        pending_list = pending.values_list('serial', flat=True)
        return list(pending_list)

    def get_commission(self, clientkey=None, serial=None):
        try:
            commission = Commission.objects.get(clientkey=clientkey,
                                                serial=serial)
        except Commission.DoesNotExist:
            raise NoCommissionError(serial)

        objs = Provision.objects.select_related('holding')
        provisions = objs.filter(serial=commission)

        ps = [p.todict() for p in provisions]

        response = {'serial':     serial,
                    'provisions': ps,
                    'issue_time': commission.issue_time,
                    }
        return response


API_Callpoint = QuotaholderDjangoDBCallpoint
