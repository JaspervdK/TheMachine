# The Fleet

The Fleet lets your Machine and the Machines of the people you work or live with help each other. It is in beta.
Machines never talk to each other directly: questions and answers pass through The Machine's relay. Hosted Machines
are on it already, and a Machine you run yourself can join too (see "A self-hosted Machine in a Fleet").

There are two kinds:

- a **Work Fleet**: you and your teammates;
- a **Family Fleet**: you and the people you live with.

You can be in one of each. Your Machine keeps the rules for each Fleet apart.

## What a Work Fleet does

1. **Who knows someone at X?** Ask from the **Fleet** tab, or with **Ask the Fleet** on a person or organisation in
   the Graph. Each teammate's Machine answers from its own graph: "knows 2 people, closest: regular, last contact
   2026-09". Names only when that teammate allows details.
2. **Meeting prep.** If you turn it on, your Machine asks your Work Fleet about the outside people in your meetings this
   week: how close each teammate is, their last contact and open promises. The answers show on the meeting's card
   ("From your Fleet: …") and go into its preparation.
3. **No double replies.** When the same email reaches you and a teammate, the first one to take it on claims it. The
   other's card says "<teammate> is on it", with **I'll handle it** to take it over.

A Family Fleet links your Machines but asks nothing yet. Its questions (who's free, the household calendar, bills and
renewals, trips, birthdays, errands, care for parents) are designed and come later.

## Answers, not data

A Machine never hands out an email, a message, a calendar entry or a file. It answers with a short summary it builds
itself:

| Question | Always | Only with Details on |
|---|---|---|
| Who knows someone at X | knows or not, closeness, last contact month, how many people | up to 5 names with their closeness and last contact |
| Meeting context | per attendee: closeness, last contact month, how many open promises each way | the name and up to 3 open promises per side |

- **Only work contacts.** A Work Fleet only hears about people seen in your work accounts (accounts with a `work:`
  boundary; without boundaries, every account not marked personal). People only seen in personal accounts never come
  up. Neither do roles, nor people with only a free-mail address unless asked by that exact address.
- **No content, ever:** no message text, subjects, files, calendar titles, notes or facts.

## You decide, per teammate

- **Off by default.** Joining a Fleet only makes your Machine reachable. The first question from a teammate waits for
  you in the Fleet tab: **Answer once**, **Always**, **Decline** or **Never**. Always and Never become your rule for
  that teammate and that kind of question.
- **You hear about it.** A waiting question sends one phone notification (if you turned them on in the dashboard
  app) and, with Telegram connected, one Telegram message with **Answer once**, **Always** and **Decline**. Questions
  that arrive within a minute share one. They say who asks and what, never anything of yours. A tap on the phone
  notification opens the question in the Fleet tab and asks you to confirm.
- **Rules:** per teammate and per kind (who knows someone, meeting context, who handles shared emails): Ask me first,
  Always answer or Never, plus Details. Shared-email claims are Off until you choose Share.
- **Only from your dashboard.** Rules and approvals can't be changed by Telegram, the command line, the API token or the
  assistant. The one exception: the buttons on the Telegram message sent for that very question, tapped by you in your
  own chat. Never is only on the dashboard. A prompt in an email can't change who gets told what.
- **A log** in the Fleet tab shows every question asked or answered, both ways.

## Safe by construction

- A teammate's question never reaches a model, a tool, the browser or Do it. It is parsed into a fixed form (an
  address, a domain or a name) and answered by plain database reads (`fleet.py`). Anything else in it is dropped.
- Answers coming back are checked against the same fixed form (closeness from a short list, a year-month, whole
  numbers, short names) before they are shown or added to a preparation, where they are marked as data from
  teammates' Machines, not instructions.
- Claims on shared emails travel as a hash keyed with the Fleet's secret, never the email.

## Who answers

The Fleet tab lists each Fleet's members with the Machine they answer with ("Self-hosted: <its name>", "Hosted: <its
address>" or "No Machine yet") and their account email; yours is marked "you, this Machine". Two members with the same
name are told apart by their email wherever only a name fits, such as a waiting question.

Each person answers with one Machine per Fleet, so teammates never get two answers from you. If you have more than one
(say a hosted one and one at home), you pick which one answers in your account. Removing someone from a Fleet takes
effect at once, and how often a Machine can ask is limited.


## A self-hosted Machine in a Fleet

A Machine you run yourself can be your Machine in a Fleet, next to teammates on hosted Machines. It needs an account
with The Machine (no subscription) and is off until you pair it:

1. In your account, make a pairing code. It works once, for 15 minutes.
2. In The Machine's folder, run `python3 cli.py fleet-pair ABCD-EFGH --label "My computer"`. This stores the
   Machine's own key, readable only by you, next to its database. A wrong, expired or already used code is refused:
   make a new one.
3. Restart the service, and pick this Machine for your Fleet in your account.

Or simply ask your Machine's AI to do it: "Pair this Machine with my Fleet: in The Machine's folder, run
`python3 cli.py fleet-pair ABCD-EFGH --label "My computer"`, then restart The Machine." Your account page shows this
with your code filled in, ready to copy.

Your Machine then shares the addresses of its work accounts with your Fleet, so your teammates' Machines recognise
them, and nothing else about you. Everything in "You decide, per teammate" and "Safe by construction" applies the same
way. You can disconnect it from your account at any time; its key then stops working.

Pairing is free. While a self-hosted Machine answers for someone in a Fleet, the relay costs €4 a month for that
member, plus VAT where it applies, paid by the Fleet's owner and confirmed before it starts; nothing while the Fleet is
free.
