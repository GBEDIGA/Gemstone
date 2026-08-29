-- ============================================================
-- GEMSTONE — Script SQL complet pour Supabase
-- Tables + relations + Row Level Security (RLS) + bucket Storage
-- À exécuter dans : Supabase > SQL Editor > New query
-- ============================================================

-- ------------------------------------------------------------
-- 1. EXTENSION nécessaire pour générer des UUID
-- ------------------------------------------------------------
create extension if not exists "pgcrypto";

-- ------------------------------------------------------------
-- 2. TABLE profils
--    Liée à auth.users (1 profil = 1 compte de connexion Supabase Auth)
-- ------------------------------------------------------------
create table if not exists public.profils (
    id          uuid primary key references auth.users (id) on delete cascade,
    nom         varchar(150) not null,
    role        varchar(20) not null check (role in ('secretaire', 'machiniste', 'caissiere', 'dg')),
    actif       boolean not null default true,
    created_at  timestamptz not null default now()
);

comment on table public.profils is 'Rôles GEMSTONE : secretaire, machiniste, caissiere, dg';

-- Contrainte métier : un seul profil actif pour "caissiere" et un seul pour "dg"
create unique index if not exists profils_unique_caissiere_active
    on public.profils (role)
    where role = 'caissiere' and actif = true;

create unique index if not exists profils_unique_dg_active
    on public.profils (role)
    where role = 'dg' and actif = true;

-- ------------------------------------------------------------
-- 3. TABLE clients
-- ------------------------------------------------------------
create table if not exists public.clients (
    id                  uuid primary key default gen_random_uuid(),
    nom                 varchar(150) not null,
    contact_whatsapp    varchar(50) not null unique,
    created_at          timestamptz not null default now()
);

-- ------------------------------------------------------------
-- 4. TABLE commandes (table centrale)
-- ------------------------------------------------------------
create table if not exists public.commandes (
    id                      uuid primary key default gen_random_uuid(),
    numero_commande         varchar(30) not null unique,
    date_heure_saisie       timestamptz not null default now(),

    client_id               uuid not null references public.clients (id),

    support_impression      varchar(50) not null,
    nombre_exemplaires      integer not null check (nombre_exemplaires > 0),
    hauteur                 numeric(8,2) not null check (hauteur > 0),
    largeur                 numeric(8,2) not null check (largeur > 0),

    fichier_path            varchar(255),

    secretaire_id           uuid not null references public.profils (id),
    machiniste_id           uuid not null references public.profils (id),
    caissiere_id            uuid references public.profils (id),

    statut                  varchar(30) not null default 'en_attente_traitement'
                             check (statut in ('en_attente_traitement', 'en_cours_impression', 'livree_payee')),

    date_debut_impression   timestamptz,
    date_livraison          timestamptz,

    mode_paiement           varchar(10) check (mode_paiement in ('momo', 'especes')),
    montant_total            numeric(12,2),

    created_at              timestamptz not null default now()
);

create index if not exists idx_commandes_machiniste on public.commandes (machiniste_id);
create index if not exists idx_commandes_secretaire on public.commandes (secretaire_id);
create index if not exists idx_commandes_statut on public.commandes (statut);

-- ------------------------------------------------------------
-- 5. TABLE historique_statuts (traçabilité, utile pour le dashboard DG)
-- ------------------------------------------------------------
create table if not exists public.historique_statuts (
    id                  uuid primary key default gen_random_uuid(),
    commande_id         uuid not null references public.commandes (id) on delete cascade,
    ancien_statut       varchar(30),
    nouveau_statut      varchar(30) not null,
    date_changement     timestamptz not null default now(),
    utilisateur_id      uuid references public.profils (id)
);

-- Trigger : enregistre automatiquement chaque changement de statut
create or replace function public.log_changement_statut()
returns trigger as $$
begin
    if (tg_op = 'UPDATE' and new.statut is distinct from old.statut) then
        insert into public.historique_statuts (commande_id, ancien_statut, nouveau_statut)
        values (new.id, old.statut, new.statut);
    end if;
    return new;
end;
$$ language plpgsql security definer;

drop trigger if exists trg_log_changement_statut on public.commandes;
create trigger trg_log_changement_statut
    after update on public.commandes
    for each row
    execute function public.log_changement_statut();

-- ------------------------------------------------------------
-- 6. BUCKET STORAGE pour les fichiers clients
-- ------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('commandes-fichiers', 'commandes-fichiers', false)
on conflict (id) do nothing;

-- ------------------------------------------------------------
-- 7. FONCTION UTILITAIRE : récupérer le rôle de l'utilisateur connecté
-- ------------------------------------------------------------
create or replace function public.mon_role()
returns varchar as $$
    select role from public.profils where id = auth.uid();
$$ language sql stable security definer;

-- ============================================================
-- 8. ROW LEVEL SECURITY (RLS)
-- ============================================================

alter table public.profils enable row level security;
alter table public.clients enable row level security;
alter table public.commandes enable row level security;
alter table public.historique_statuts enable row level security;

-- ---------------- PROFILS ----------------
-- Tout utilisateur connecté peut lire les profils (nécessaire pour les listes déroulantes)
create policy "profils_lecture_authentifies"
    on public.profils for select
    using (auth.role() = 'authenticated');

-- Seul le DG peut créer / modifier / désactiver des profils
create policy "profils_gestion_dg"
    on public.profils for all
    using (public.mon_role() = 'dg')
    with check (public.mon_role() = 'dg');

-- ---------------- CLIENTS ----------------
-- Secrétaire, caissière et DG peuvent lire/écrire les clients
create policy "clients_lecture_ecriture"
    on public.clients for all
    using (public.mon_role() in ('secretaire', 'caissiere', 'dg'))
    with check (public.mon_role() in ('secretaire', 'caissiere', 'dg'));

-- ---------------- COMMANDES ----------------
-- Secrétaire : peut créer des commandes et lire les siennes
create policy "commandes_secretaire_lecture"
    on public.commandes for select
    using (
        public.mon_role() = 'secretaire' and secretaire_id = auth.uid()
        or public.mon_role() in ('caissiere', 'dg')
        or (public.mon_role() = 'machiniste' and machiniste_id = auth.uid())
    );

create policy "commandes_secretaire_creation"
    on public.commandes for insert
    with check (public.mon_role() = 'secretaire' and secretaire_id = auth.uid());

-- Machiniste : peut mettre à jour le statut uniquement de ses commandes assignées
create policy "commandes_machiniste_maj"
    on public.commandes for update
    using (public.mon_role() = 'machiniste' and machiniste_id = auth.uid())
    with check (public.mon_role() = 'machiniste' and machiniste_id = auth.uid());

-- Caissière : peut mettre à jour statut/paiement sur toute commande en cours
create policy "commandes_caissiere_maj"
    on public.commandes for update
    using (public.mon_role() = 'caissiere')
    with check (public.mon_role() = 'caissiere');

-- DG : accès total (lecture + écriture) en correction/supervision
create policy "commandes_dg_tout"
    on public.commandes for all
    using (public.mon_role() = 'dg')
    with check (public.mon_role() = 'dg');

-- ---------------- HISTORIQUE_STATUTS ----------------
-- Lecture seule, réservée à la caissière et au DG (suivi/analytics)
create policy "historique_lecture_caissiere_dg"
    on public.historique_statuts for select
    using (public.mon_role() in ('caissiere', 'dg'));

-- ============================================================
-- 9. POLICIES STORAGE (bucket commandes-fichiers)
-- ============================================================

-- Upload : réservé aux secrétaires
create policy "storage_upload_secretaire"
    on storage.objects for insert
    with check (
        bucket_id = 'commandes-fichiers'
        and public.mon_role() = 'secretaire'
    );

-- Téléchargement : machiniste, caissière et DG
create policy "storage_lecture_roles_autorises"
    on storage.objects for select
    using (
        bucket_id = 'commandes-fichiers'
        and public.mon_role() in ('machiniste', 'caissiere', 'dg')
    );

-- ============================================================
-- FIN DU SCRIPT
-- Prochaine étape : créer les comptes dans Authentication > Users,
-- puis ajouter la ligne correspondante dans la table "profils"
-- avec le bon "role" (secretaire / machiniste / caissiere / dg).
-- ============================================================
