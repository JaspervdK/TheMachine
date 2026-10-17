"""Stripe: billing signals worth a look (failed payments, disputes, cancellations, big new subscriptions).

Read-only: GET on the account, events and customer endpoints only. Never writes, refunds or retries.

Config, with a restricted read-only key (Events, Customers, Account: Read) in a credential file:
    {"id": "billing", "service": "stripe", "transport": "api", "boundary": "work",
     "credential": "stripe", "stripe_account": "acct_...", "poll_seconds": 900}
or through a Maton Stripe connection:
    {"id": "billing", "service": "stripe", "transport": "maton", "boundary": "work",
     "credential": "maton", "credential_variable": "MATON_API_KEY", "connection_id": "...",
     "stripe_account": "acct_...", "poll_seconds": 900}
Optional: "large_subscription_monthly" (default 500, in the price currency): new subscriptions
at or above this monthly value become events; smaller ones are routine.
"""
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

KINDS = {'stripe.event': 'Stripe'}
# For `assistant connect stripe`: the account it adds and what it asks (see docs/adding-a-source.md).
SETUP = {'label': 'Stripe', 'account': {'id': 'billing', 'service': 'stripe', 'transport': 'api', 'boundary': 'work', 'credential': 'stripe',
                     'poll_seconds': 900},
         'note': 'Create a restricted key in Stripe with read access to Events, Customers and Account only.',
         'ask': [('stripe_account', 'Stripe account ID (acct_…)')],
         'secret': 'Restricted Stripe key (rk_…)',
         'maton': 'Maton Stripe connection ID'}
TYPES = ('invoice.payment_failed', 'customer.subscription.deleted', 'customer.subscription.updated',
         'customer.subscription.created', 'charge.dispute.created')
PATHS = ('v1/account', 'v1/events', 'v1/customers/')


class Source:
    def __init__(self, config, account, helpers):
        self.config, self.account, self.helpers = config, account, helpers
        self.customers = {}
        self.verified = False

    def secret(self):
        secret = self.helpers.credential(self.config, self.account['credential'])
        variable = self.account.get('credential_variable')
        if variable:
            matches = re.findall(r'^' + re.escape(variable) + r'=(.*)$', secret, re.M)
            if len(matches) != 1:
                raise self.helpers.SourceError('stripe_credential_unavailable')
            secret = matches[0].strip().strip('"\'')
        return secret

    def get(self, path, params=None):
        SourceError = self.helpers.SourceError
        if not path.startswith(PATHS):
            raise SourceError('stripe_path_not_allowed')
        maton = self.account.get('transport') == 'maton'
        base = 'https://api.maton.ai/stripe/' if maton else 'https://api.stripe.com/'
        headers = {'Authorization': 'Bearer ' + self.secret(), 'Accept': 'application/json'}
        if maton:
            headers['Maton-Connection'] = self.account['connection_id']
        url = base + path + ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as response:
                if maton and response.headers.get('Maton-Connection') != self.account['connection_id']:
                    raise SourceError('connection_identity_mismatch')
                return json.load(response)
        except urllib.error.HTTPError as error:
            retry = error.headers.get('Retry-After') if error.headers else None
            raise SourceError('stripe', error.code, min(3600, max(60, int(retry))) if retry and retry.isdigit() else None) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SourceError('network:' + type(error).__name__ + ':stripe_read') from None
        except ValueError:
            raise SourceError('stripe_invalid_json') from None

    def verify(self):
        """The key must belong to the configured Stripe account, not just any account."""
        if not self.verified:
            if self.get('v1/account').get('id') != self.account['stripe_account']:
                raise self.helpers.SourceError('account_identity_mismatch')
            self.verified = True

    def customer(self, customer_id):
        if not isinstance(customer_id, str) or not customer_id.startswith('cus_'):
            return {}
        if customer_id not in self.customers:
            try:
                self.customers[customer_id] = self.get('v1/customers/' + customer_id)
            except self.helpers.SourceError as error:
                if error.status != 404:
                    raise
                self.customers[customer_id] = {}
        return self.customers[customer_id]


def day(timestamp):
    return dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc).date().isoformat()


def money(amount, currency):
    return f"{(amount or 0) / 100:,.2f} {str(currency or '').upper()}".replace('.00 ', ' ')


def monthly(subscription):
    """What a subscription brings in per month, from its items (yearly prices divided by 12)."""
    total, currency = 0, subscription.get('currency')
    for item in (subscription.get('items') or {}).get('data', []):
        price = item.get('price') or {}
        recurring = price.get('recurring') or {}
        months = {'day': 1 / 30, 'week': 7 / 30, 'month': 1, 'year': 12}.get(recurring.get('interval'), 1) * (recurring.get('interval_count') or 1)
        total += (price.get('unit_amount') or 0) * (item.get('quantity') or 1) / months
        currency = price.get('currency') or currency
    return round(total), currency


def large(account, subscription):
    return monthly(subscription)[0] >= 100 * float(account.get('large_subscription_monthly', 500))


def cancelling(obj, before):
    return before.get('cancel_at_period_end') is False and bool(obj.get('cancel_at_period_end'))


def overdue(obj, before):
    return 'status' in before and obj.get('status') in ('past_due', 'unpaid')


def event(account, item, source, helpers):
    """One Stripe event worth a look, or None for the routine ones (most subscription updates)."""
    kind, obj = item.get('type'), (item.get('data') or {}).get('object') or {}
    before = (item.get('data') or {}).get('previous_attributes') or {}
    if kind == 'customer.subscription.updated' and not (cancelling(obj, before) or overdue(obj, before)):
        return None
    if kind == 'customer.subscription.created' and not large(account, obj):
        return None
    if kind not in TYPES:
        return None
    customer = source.customer(obj.get('customer')) if not obj.get('customer_name') else {}
    who = obj.get('customer_name') or customer.get('name') or obj.get('customer_email') or customer.get('email') or 'A customer'
    email = obj.get('customer_email') or customer.get('email') or ''
    dashboard = 'https://dashboard.stripe.com/' + ('' if item.get('livemode', True) else 'test/')
    if kind == 'invoice.payment_failed':
        title = f"Payment failed: {who}, {money(obj.get('amount_due'), obj.get('currency'))}"
        retry = obj.get('next_payment_attempt')
        lines = [f"Attempt {obj.get('attempt_count') or 1}; " + (f"Stripe retries {day(retry)}." if retry else 'no automatic retry scheduled.'),
                 f"Reason: {obj.get('billing_reason') or 'unknown'}."]
        url = dashboard + 'invoices/' + str(obj.get('id'))
    elif kind == 'charge.dispute.created':
        due = (obj.get('evidence_details') or {}).get('due_by')
        title = f"Dispute: {who}, {money(obj.get('amount'), obj.get('currency'))}"
        lines = [f"Reason: {obj.get('reason') or 'unknown'}." + (f" Evidence due {day(due)}." if due else '')]
        url = dashboard + 'disputes/' + str(obj.get('id'))
    else:
        amount, currency = monthly(obj)
        value = f"{money(amount, currency)}/month"
        url = dashboard + 'subscriptions/' + str(obj.get('id'))
        if kind == 'customer.subscription.deleted':
            title = f"Subscription ended: {who}, {value}"
            reason = obj.get('cancellation_details') or {}
            lines = [f"Reason: {reason.get('reason') or 'not given'}." + (f" Feedback: {reason.get('feedback')}." if reason.get('feedback') else '') +
                     (f" Comment: {reason.get('comment')}" if reason.get('comment') else '')]
        elif kind == 'customer.subscription.created':
            title = f"New subscription: {who}, {value}"
            lines = [f"Status: {obj.get('status')}."]
        elif cancelling(obj, before):
            title = f"Cancellation scheduled: {who}, {value}"
            end = obj.get('cancel_at') or obj.get('current_period_end')
            lines = [f"Ends {day(end)}." if end else 'Ends at the end of the period.']
        else:
            title = f"Subscription {obj['status'].replace('_', ' ')}: {who}, {value}"
            lines = [f"Was {before['status']}."]
    body = '\n'.join(x for x in [title + '.', email and 'Email: ' + email] + lines if x)
    return {'account_id': account['id'], 'source_id': item['id'], 'kind': 'stripe.event', 'occurred_at': helpers.iso(int(item['created'])),
            'title': title[:1000], 'body': body[:40000], 'deleted': False,
            'metadata': {'type': kind, 'customer_id': obj.get('customer'), 'from': email, 'customer_name': who, 'source_url': url}}


def sync(store, config, account, helpers, source=None):
    source = source or Source(config, account, helpers)
    source.verify()
    state = store.checkpoint(account['id']) or {}
    since = int(state.get('since') or time.time() - 86400 * int(config.get('bootstrap_days', 7)))
    window = state.get('catchup') or {'after': max(0, since - 1), 'before': int(time.time()), 'newest': since}
    params = {'limit': 100, 'types[]': list(TYPES), 'created[gte]': window['after'], 'created[lte]': window['before']}
    if window.get('cursor'):
        params['starting_after'] = window['cursor']
    read, count, more = 0, 0, False
    for _ in range(10):  # newest first; a quiet account needs one page
        page = source.get('v1/events', params)
        items = page.get('data', [])
        if page.get('has_more') and not items:
            raise helpers.SourceError('stripe_invalid_page')
        events = [e for e in (event(account, item, source, helpers) for item in reversed(items)) if e]
        read += len(items)
        count += len(events)
        window['newest'] = max([window['newest']] + [int(item.get('created') or 0) for item in items])
        more = bool(page.get('has_more'))
        if more:
            cursor = items[-1]['id']
            if cursor == window.get('cursor'):
                raise helpers.SourceError('stripe_invalid_cursor')
            window['cursor'] = cursor
            state.update(mode='incremental', since=since, catchup=window)
            params['starting_after'] = cursor
        else:
            state.update(mode='incremental', since=window['newest'])
            state.pop('catchup', None)
        state.update(coverage_complete=not more, coverage='Older Stripe events are still being collected.' if more else 'Stripe event pages caught up.')
        store.commit_page(account['id'], state, events)
        if not more:
            break
    return {'mode': 'incremental', 'events': count, 'read': read, 'more': more}
