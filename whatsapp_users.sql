create extension if not exists pgcrypto;

create table if not exists public.whatsapp_users (
  id uuid not null default gen_random_uuid(),
  name text null,
  email text null,
  mobile text not null,
  addresses jsonb not null default '{}'::jsonb,
  wishlist jsonb not null default '[]'::jsonb,
  total_orders integer not null default 0,
  created_at timestamp with time zone not null default now(),
  constraint whatsapp_users_pkey primary key (id),
  constraint whatsapp_users_mobile_key unique (mobile)
);

do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'whatsapp_users'
      and column_name = 'wishlist'
      and data_type <> 'jsonb'
  ) then
    alter table public.whatsapp_users
      alter column wishlist drop default;

    alter table public.whatsapp_users
      alter column wishlist type jsonb
      using case
        when wishlist is null then '[]'::jsonb
        else to_jsonb(wishlist)
      end;
  end if;
end $$;

alter table public.whatsapp_users
  alter column wishlist set default '[]'::jsonb;

create table if not exists public.whatsapp_orders (
  id uuid not null default gen_random_uuid(),
  user_mobile text not null,
  customer_name text null,
  product_name text not null,
  quantity integer not null default 1,
  size text null,
  address text null,
  order_status text not null default 'pending',
  created_at timestamp with time zone not null default now(),
  updated_at timestamp with time zone not null default now(),
  constraint whatsapp_orders_pkey primary key (id)
);
