-- 0002: let the single OTP operator manage monitoring targets from the dashboard.
-- The new dashboard "管理" tab does CRUD on entities / keywords / sources; in 0001
-- the authenticated role was read-only (except alerts.status). mentions, sentiment,
-- aggregates and pipeline_runs STAY read-only. Safe because only one user can sign
-- in (email OTP, shouldCreateUser:false) and anon still has zero grants/policies.

grant insert, update, delete on entities to authenticated;
grant insert, update, delete on keywords to authenticated;
grant insert, update, delete on sources  to authenticated;

-- entities / keywords / sources ids are GENERATED ALWAYS AS IDENTITY, so inserts
-- omit id (PostgREST does this automatically) — no sequence grant needed.

create policy auth_ins on entities for insert to authenticated with check (true);
create policy auth_upd on entities for update to authenticated using (true) with check (true);
create policy auth_del on entities for delete to authenticated using (true);

create policy auth_ins on keywords for insert to authenticated with check (true);
create policy auth_upd on keywords for update to authenticated using (true) with check (true);
create policy auth_del on keywords for delete to authenticated using (true);

create policy auth_ins on sources for insert to authenticated with check (true);
create policy auth_upd on sources for update to authenticated using (true) with check (true);
create policy auth_del on sources for delete to authenticated using (true);
