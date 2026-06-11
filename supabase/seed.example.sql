-- FICTIONAL example seed. Real targets are entered directly in Supabase Studio
-- (Table Editor) and never committed to this public repo. See docs/SOP.md.

insert into entities (slug, name, side) values
  ('acme-handheld',  'Acme Handheld X',   'ours'),
  ('acme-keeb',      'Acme Keeb 75',      'ours'),
  ('zenith-deck',    'Zenith Deck',       'competitor'),
  ('borealis-pad',   'Borealis Pad Pro',  'competitor');

insert into keywords (entity_id, keyword, match_type) values
  ((select id from entities where slug = 'acme-handheld'), 'acme handheld', 'phrase'),
  ((select id from entities where slug = 'acme-handheld'), '掌上遊戲機',     'phrase'), -- CJK → substring match
  ((select id from entities where slug = 'acme-keeb'),     'keeb 75',       'phrase'),
  ((select id from entities where slug = 'zenith-deck'),   'zenith deck',   'phrase'),
  ((select id from entities where slug = 'borealis-pad'),  'borealis pad',  'phrase');

insert into sources (platform, kind, source_key, config) values
  ('reddit',  'subreddit', 'handheldgaming',                 '{}'),
  ('reddit',  'subreddit', 'MechanicalKeyboards',            '{}'),
  ('reddit',  'search',    '"acme handheld" OR "zenith deck"', '{}'),
  ('youtube', 'channel',   'UCxxxxxxxxxxxxxxxxxxxxxx',       '{}'),
  ('youtube', 'search',    '"acme handheld" review',         '{}');
