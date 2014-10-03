# Administrator interface API

The administrator interface runs over HTTP. Some administrator interface
actions are available via a RESTful API, which is documented here:
 
## Authentication

Authenticate using HTTP Digest Auth, using your regular admin interface username and password

## Data Formats

All data submitted and returned is JSON. When making a `POST`, be sure to set the `Content-Type` 
header to `application/json`

Price and quantity values are in user-friendly formats, not internal wire representations.

If an error occurs, the following is returned:

```json
{
    error: "message"
}
```

## Calls

### GET /api/withdrawals

List all pending withdrawal requests. Returns an array of withdrawal requests, each of the form:

```json
{
    id: 2342
    username: "username"
    address: "BTC address or Wire Transfer / other instructions"
    contract: "TICKER"
    amount: 2394.34
    entered: 209384234243
}
```

### GET /api/deposits

List all active deposit addresses. Returns an array of deposit addresses, each of the form:

```json
{
    id: 2342
    username: "username"
    contract: "TICKER"
    address: "BTC address or unique identifier"
    active: true
    accounted_for: 342.54
}
```

### POST /api/process_withdrawal

Process a withdrawal request

Data submitted is of the form:

```json
{
    id: 2342
    online: true
    cancel: false
}
```

If `online` is true, then a crypto-currency withdrawal is performed using the on-line wallet. If not,
it is assumed that the withdrawal was performed offline, using the cold storage wallet or fiat currency
via wire transfer/check/etc.

If `cancel` is true, then the withdrawal request is cancelled and funds are returned to the requesting
user's account.

On success:

```json
{
    result: true
}
```

### POST /api/manual_deposit

Mark that cash has been deposited into a user's account via a given deposit address.

Data submitted is of the form:

```json
{
    address: 'BTC address or unique identifier'
    quantity: 2342.434
}
```

On success:

```json
{
    result: true
}
```
    