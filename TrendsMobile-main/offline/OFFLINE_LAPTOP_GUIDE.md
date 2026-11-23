# Shop laptops that keep working without internet

During load-shedding a shop's laptop stays on through its UPS, but the internet often goes down. A shop laptop set up
as described here runs the **whole ERP on the laptop itself**, so sales, purchases, returns, payments and expenses
continue with no internet at all, for as long as needed. When the internet returns, the laptop sends its work to the
cloud ERP and receives everything that changed there, automatically and in the background.

```
 Shop laptop (on UPS)                                   Cloud ERP (Supabase + Vercel)
 ┌──────────────────────────────┐   when online        ┌──────────────────────────────┐
 │ Browser → http://localhost   │  ── its shop's ───►  │ owner, reports, all shops     │
 │ ERP + PostgreSQL (Docker)    │     work              │ website + online orders       │
 │ sync agent (every 30 s)      │  ◄── everything ───  │ prices, users, settings       │
 └──────────────────────────────┘      else            └──────────────────────────────┘
```

## What works where

| On the shop laptop (online or offline) | Only in the cloud ERP (needs internet) |
| --- | --- |
| New Sale / POS, credit sales, customer payments | Prices and product settings, photos, the website |
| Buying phones and accessories, supplier payments | Shops, staff, logins, payment methods, accounts |
| Returns, exchanges, stock adjustments | Locking a month, year-end, owner journals |
| Expenses, daily closing, till shifts | Stock transfers between shops, online orders |
| Reports for the shop (as of the last sync) | Reports across all shops, right up to date |

If something on the right is tried on the laptop, it says **"Do this in the cloud ERP"** and nothing is saved.

The header always shows the laptop's state: **Synced with the cloud**, **Syncing · 3 waiting**, or
**Offline · 12 waiting**. "Waiting" work is safe on the laptop; it is sent as soon as the internet returns.

## Rules the shop should know

1. **Sign in once while online** on the laptop with each login that will use it. After that, sign-in works offline.
   A password changed in the cloud works on the laptop after its next online sign-in.
2. **Keep Docker Desktop running** (set it to start with Windows). It restarts the ERP by itself after a power cut.
3. **The website does not sell this shop's stock while the laptop is offline** (5 minutes without contact), so a phone
   sold at the counter cannot also be sold online. It reappears automatically when the laptop reconnects.
4. **The owner cannot lock a month or close a year** for a shop until its laptop has synced. This is deliberate.

## Conflicts

Rarely, the same record is changed in two places while a laptop is offline. For example, the website reserves a phone
just before the shop sells it for cash, or the same IMEI is bought in two shops. The cloud then **holds** that piece of
work instead of overwriting either side. The owner sees it under **Settings → Shop Laptops & Sync** and chooses:

* **Apply the shop's version**: for example, the cash sale really happened.
* **Keep the cloud's version**: the laptop is corrected at its next sync.

The laptop keeps working the whole time. Work that does not touch the held record keeps flowing.

## Setting up a laptop (support)

On the cloud side (once per laptop), with the cloud database settings in the environment:

```sh
python manage.py migrate
python manage.py sync_register_node --branch SHOPCODE --name "Gulberg counter laptop"
```

This prints `SYNC_NODE_KEY=…` once. Keep it only on that laptop.

On the laptop (Windows 10/11, 8 GB RAM, Docker Desktop installed, virtualization/WSL2 enabled), from the `offline`
folder, while online:

```powershell
.\Setup-ShopLaptop.ps1 -HubUrl https://YOUR-ERP.vercel.app -NodeKey THE-KEY `
    -SupabaseUrl https://PROJECTREF.supabase.co -SupabasePublishableKey YOUR-PUBLISHABLE-KEY
```

The script builds the application image for the laptop's processor, starts PostgreSQL and the ERP, copies the cloud
data, and starts the sync agent. After that, `Start-ShopLaptop.ps1` starts it again if ever needed.

Run `python manage.py sync_prune` on the cloud side weekly to keep the sync history short.

## How it works (for developers)

* Code: the `sync` app. Triggers (`sync/triggers.py`) record every changed row in `sync_change`, grouped by transaction.
  `sync/registry.py` lists which tables a laptop sends (its shop's documents, ledger, stock, phones, customers and new
  catalogue entries) and which are cloud-only. On a laptop the database refuses local changes to cloud-only tables.
* Rows travel whole and are applied idempotently; transactions that touch the same rows travel together, so a sale
  never arrives without the phone it sold. Each laptop numbers new rows from `node_no × 10¹²`, so ids never collide.
* New phone models and variants get the same id on every laptop for the same name, so two shops that both add
  "iPhone 15 128GB Black" offline end up with one variant.
* The cloud checks every pushed transaction for: a row changed in the cloud since the laptop last synced (unless both
  sides are identical), another shop's records, and dates inside a locked period. Failing work becomes a conflict.
* Tests: `python manage.py test sync` runs a laptop and a cloud database side by side.

## Known limits

* Receipt numbers are random codes as before, not one running sequence per shop.
* PTA evidence photos uploaded on a laptop stay on that laptop for now; the record of the check is synced.
* Two shops adding an accessory with the same new name offline get two accessory products (phones are merged).
* Each laptop holds a copy of the whole business database (every shop), so the owner can also sign in on it. Shop
  logins still see only their own shop, but treat every laptop as holding all shops' data: keep Windows sign-in and
  disk encryption (BitLocker) on.
* A laptop's sync key can send its shop's work and new catalogue entries. If a laptop is lost, run
  `python manage.py sync_disable_node LAPTOP-NUMBER` on the cloud side and change that shop's passwords.
