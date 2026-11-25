-- One-time setup by a DBA, connected to the chem-os database as an admin user.
-- Creates a least-privilege user for the shipping module instead of sharing the
-- admin/chem-os credentials.
--
-- Replace the password before running. Afterwards, connect as shipping_scraper
-- and run `python main.py migrate`: tables it creates are owned by shipping_scraper.

-- 1. The pg_trgm extension needs elevated privileges; create it once here.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. The service user.
CREATE ROLE shipping_scraper LOGIN PASSWORD 'CHANGE_ME';
GRANT CONNECT ON DATABASE chemos_dev TO shipping_scraper;
GRANT USAGE, CREATE ON SCHEMA public TO shipping_scraper;

-- 3. Read-only access to chem-os's ship master, to find my-ships.
GRANT SELECT ON shipwithid TO shipping_scraper;

-- 4. chem-os reads the scraped data (adjust the role name to chem-os's DB user).
-- Run after the first `migrate`, connected as shipping_scraper:
--   GRANT SELECT ON mainshipdata TO <chem_os_user>;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO <chem_os_user>;

-- 5. If the scraper tables already exist (created by an earlier version under
-- another user), hand them over so migrate can alter them:
--   ALTER TABLE ships        OWNER TO shipping_scraper;
--   ALTER TABLE vessels      OWNER TO shipping_scraper;
--   ALTER TABLE shippingport OWNER TO shipping_scraper;
--   ALTER TABLE positions    OWNER TO shipping_scraper;
--   ALTER TABLE voyages      OWNER TO shipping_scraper;
--   ALTER TABLE snapshots    OWNER TO shipping_scraper;
--   ALTER TABLE scrape_log   OWNER TO shipping_scraper;
--   ALTER TABLE mainshipdata OWNER TO shipping_scraper;
--   (serial sequences follow their table's owner automatically)
