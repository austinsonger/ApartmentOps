# Platform notes: Yad2 (Israel)

Historical observations from past Yad2 rental hunts, preserved so an
Israeli search does not have to rediscover them. Treat every URL form,
field path, and market heuristic here as something to re-verify against a
live page (the extractor probe in `apartmentops-scan` Phase 2) before
relying on it. Mechanisms that apply to every platform live in
`../collection-playbook.md`.

## Locale

Set the `locale` block in `config.yml` (see `../contracts.md`):
`currency: ILS`, `timezone: Asia/Jerusalem`, `room_convention: il_rooms`.
Never silently switch country or currency.

- Israeli "rooms" count the living room: a 2-room flat is a one-bedroom,
  3 rooms is a two-bedroom. Store the listing's count as `rooms` and only
  derive `beds` (rooms - 1) as INFERRED. A bedroom filter applied to rooms
  over-filters.
- Ask for a rent floor deliberately: a whole-apartment ad far below the
  local market (historically under roughly 5,000 ILS in central cities) is
  usually a room in a shared flat. The floor is what removes them.
- A broker fee is typically one month's rent. Ask whether it is a
  deal-breaker (`budget.broker_fee.mode: hard` skips agency ads) or a
  negative (`bonus`).
- Size is in square meters (`size_sqm`).

## Recurring charges

| Hebrew | Meaning | Billing |
|---|---|---|
| ועד בית (vaad bayit) | building committee fee | usually monthly |
| ארנונה (arnona) | municipal property tax | usually bi-monthly; never add the bill as a monthly charge |

Record each as a fee object with its `billing_period`
(`quality.normalize_fee` spreads it per month). A value of 9999 is a
placeholder, not a number. A vaad of 800-1,900 ILS makes true cost far
above the rent; always show rent + vaad + arnona.

## Search pages

Plain HTTP requests have historically returned a bot-manager page (HTTP
200, no payload); use a real browser tool. Use the canonical URL form;
short forms like `?city=X&rooms=2-3` redirected and sometimes dropped
every filter (tell: a result count in the thousands).

```
https://www.yad2.co.il/realestate/rent/<region-slug>?area=<A>&city=<C>
  &minPrice=<MIN>&maxPrice=<MAX>&minRooms=<MIN>&maxRooms=<MAX>
  &minSquaremeter=<MIN>&maxSquaremeter=1000[&elevator=1...]
```

Read `city=` and `area=` codes from a real search URL for that city -
never guess them. Hard requirements (elevator, parking, safe room)
become query parameters; preferences become scoring inputs.

Observed feed payload (`__NEXT_DATA__`): separate `private` and `agency`
arrays - read both; `private` means no broker fee. Per item: `token`,
`address.neighborhood.text`, `address.coords.lat/lon`, `price`,
`additionalDetails.{roomsCount, squareMeter, propertyCondition}`,
`metaData.images`. The feed carried no dates, and date sorting did not
work. `pagination.totalPages` gave the page count; page sizes varied.

## Item pages

Observed in `props.pageProps.dehydratedState.queries[*].state.data` (the
query whose data has `adNumber`; none means the ad was removed):

| Path | Meaning |
|---|---|
| `price` | rent (0 = not stated: skip) |
| `additionalDetails.roomsCount`, `.squareMeter` | rooms, size_sqm |
| `address.street.text`, `address.house.number`, `.house.floor` | address, floor (house numbers are hand-typed and unreliable) |
| `address.coords.lat/lon` | coordinates |
| `houseCommittee`, `propertyTax` | vaad bayit, arnona |
| `inProperty.includeElevator / includeParking / includeSecurityRoom` | elevator, parking, safe room (mamad) |
| `additionalDetails.balconiesCount`, `.propertyCondition.text`, `.entranceDate` | balconies, condition, move-in date |
| `adType` (`private`/`commercial`), `subcategoryId` (2 private, 6 agency) | advertiser type |
| `customer.agencyName` / `customer.name` | advertiser |
| `dates.createdAt` | full local-time publication timestamp: keep it whole as `published_at` with `publication_timezone: Asia/Jerusalem` |
| `dates.updatedAt` | refresh time; never freshness. Agencies bump old ads |
| `additionalDetails.property.text` | property type: `סאבלט` is a sublet (skip); `גג/ פנטהאוז` roof/penthouse (heat and insulation); `דירת גן` garden flat (damp, security, stated area may include yard) |

Sublets appear only on the item page, not the feed. A client-rendered
`<title>` check for sublets silently passes everything, because fetched
HTML has no title.

Past bot-wall titles included "Radware Bot Manager Captcha" and
"ShieldSquare Captcha": stop, record the blocker, report coverage.

## Notes sweep (Hebrew relative time)

`quality.relative_time_hits` already catches מהיום, הטרייה, להזדרז,
אתמול and היום alongside the English phrases.
