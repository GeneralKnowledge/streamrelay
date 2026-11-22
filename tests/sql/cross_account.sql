-- Cross-account isolation at the database level. Run after schema.sql:
--   psql -v ON_ERROR_STOP=1 -f docs/architecture/schema.sql -f tests/sql/cross_account.sql
-- Each forbidden insert runs in a savepoint and must fail; the script raises if any succeeds.
BEGIN;
INSERT INTO users VALUES ('00000000-0000-0000-0000-00000000000a', 'a@example.test', 'x');
INSERT INTO users VALUES ('00000000-0000-0000-0000-00000000000b', 'b@example.test', 'x');
INSERT INTO devices (id, owner_id, name, platform, token_hash)
  VALUES ('00000000-0000-0000-0000-0000000000da', '00000000-0000-0000-0000-00000000000a', 'A phone', 'test', 'ta'),
         ('00000000-0000-0000-0000-0000000000db', '00000000-0000-0000-0000-00000000000b', 'B phone', 'test', 'tb');
INSERT INTO destinations (id, owner_id, kind, name, ingest_url, stream_key_enc, stream_key_nonce)
  VALUES ('00000000-0000-0000-0000-0000000000fb', '00000000-0000-0000-0000-00000000000b', 'twitch', 'B twitch', 'rtmps://ingest.example/app', 'k', 'n');
INSERT INTO broadcasts (id, owner_id, device_id, client_ref, title)
  VALUES ('00000000-0000-0000-0000-0000000000ba', '00000000-0000-0000-0000-00000000000a',
          '00000000-0000-0000-0000-0000000000da', '00000000-0000-0000-0000-000000000001', 'A broadcast');

CREATE FUNCTION must_fail(sql text, label text) RETURNS void AS $$
BEGIN
  BEGIN
    EXECUTE sql;
  EXCEPTION WHEN others THEN
    RAISE NOTICE 'ok: % rejected (%)', label, SQLERRM;
    RETURN;
  END;
  RAISE EXCEPTION 'FAIL: % was accepted', label;
END $$ LANGUAGE plpgsql;

SELECT must_fail($q$INSERT INTO broadcasts (id, owner_id, device_id, client_ref, title)
  VALUES (gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000db', gen_random_uuid(), 'x')$q$,
  'broadcast owned by A using B''s device');
SELECT must_fail($q$INSERT INTO broadcast_destinations (broadcast_id, destination_id, owner_id)
  VALUES ('00000000-0000-0000-0000-0000000000ba', '00000000-0000-0000-0000-0000000000fb', '00000000-0000-0000-0000-00000000000a')$q$,
  'A''s broadcast using B''s destination (owner A)');
SELECT must_fail($q$INSERT INTO broadcast_destinations (broadcast_id, destination_id, owner_id)
  VALUES ('00000000-0000-0000-0000-0000000000ba', '00000000-0000-0000-0000-0000000000fb', '00000000-0000-0000-0000-00000000000b')$q$,
  'A''s broadcast using B''s destination (owner B)');
SELECT must_fail($q$INSERT INTO destinations (id, owner_id, kind, name, ingest_url, stream_key_enc, stream_key_nonce)
  VALUES (gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', 'twitch', 'plain', 'rtmp://ingest.example/app', 'k', 'n')$q$,
  'Twitch destination over plain rtmp');
SELECT must_fail($q$INSERT INTO destinations (id, owner_id, kind, name, ingest_url, stream_key_enc, stream_key_nonce)
  VALUES (gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', 'rtmp', 'plain', 'rtmp://obs.local/live', 'k', 'n')$q$,
  'custom plain rtmp without opt-in');
-- allowed: custom plain rtmp with explicit opt-in
INSERT INTO destinations (id, owner_id, kind, name, ingest_url, stream_key_enc, stream_key_nonce, insecure_ok)
  VALUES (gen_random_uuid(), '00000000-0000-0000-0000-00000000000a', 'rtmp', 'OBS', 'rtmp://obs.local/live', 'k', 'n', true);
ROLLBACK;
