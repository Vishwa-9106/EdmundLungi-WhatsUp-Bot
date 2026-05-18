create extension if not exists pgcrypto;

create table if not exists public.whatsapp_users (
  id uuid primary key default gen_random_uuid(),
  name text,
  email text,
  mobile text unique,
  address text,
  created_at timestamptz default now()
);
