"""
GEMSTONE — Gestion des commandes (Atelier d'impression)
Application Streamlit unique (tout-en-un) connectée à Supabase.

Rôles gérés dans ce même fichier :
- secretaire : saisie des commandes reçues via WhatsApp
- machiniste  : téléchargement du fichier + bascule automatique du statut
- caissiere   : suivi du statut + clôture livraison/paiement
- dg          : tableau de bord global + gestion des profils employés

Prérequis Supabase : tables 'profils', 'clients', 'commandes'
                      + bucket Storage 'commandes-fichiers'
                      (voir supabase_schema.sql pour la création complète).

Secrets requis (.streamlit/secrets.toml ou Streamlit Cloud > Settings > Secrets) :
- SUPABASE_URL         : URL du projet Supabase
- SUPABASE_KEY         : clé publishable (sb_publishable_...) — utilisée pour toutes
                          les opérations normales, protégée par les policies RLS.
- SUPABASE_SERVICE_KEY : clé secrète (sb_secret_...) — utilisée UNIQUEMENT côté
                          serveur pour la création/suppression de comptes employés
                          par le DG. Ne jamais l'exposer côté client : ici c'est sûr
                          car Streamlit exécute tout le code côté serveur, jamais
                          dans le navigateur.
"""

import base64
import re
import unicodedata
from datetime import datetime

import pandas as pd
import streamlit as st
from supabase import create_client, Client, ClientOptions

# ============================================================
# CONFIGURATION GÉNÉRALE
# ============================================================

st.set_page_config(
    page_title="GEMSTONE - Gestion des commandes",
    page_icon="💎",
    layout="wide",
)

STATUT_EN_ATTENTE = "en_attente_traitement"
STATUT_EN_COURS = "en_cours_impression"
STATUT_LIVREE = "livree_payee"

STATUT_LABELS = {
    STATUT_EN_ATTENTE: "🟠 En attente de traitement",
    STATUT_EN_COURS: "🔵 En cours d'impression",
    STATUT_LIVREE: "✅ Livrée et payée",
}

SUPPORTS_IMPRESSION = [
    "Bâche mate",
    "Bâche brillante",
    "Opaque",
    "Transparent",
    "PVC",
    "One Way",
    "Réfléchissant",
    "DTF",
]

ROLES_GERES_PAR_DG = ["secretaire", "machiniste", "caissiere"]
ROLE_LABELS = {
    "secretaire": "Secrétaire",
    "machiniste": "Machiniste",
    "caissiere": "Caissière",
    "dg": "Directeur Général",
}


# ============================================================
# CONNEXION SUPABASE
# ============================================================

@st.cache_resource
def get_supabase_client() -> Client:
    """Client 'normal', utilisé pour toutes les opérations soumises aux policies RLS."""
    url = st.secrets["SUPABASE_URL"].strip()
    key = st.secrets["SUPABASE_KEY"].strip().replace("\n", "").replace(" ", "")
    return create_client(url, key)


@st.cache_resource
def get_supabase_admin_client() -> Client:
    """Client 'admin', utilisé UNIQUEMENT pour créer/supprimer des comptes utilisateurs.
    Reste côté serveur en permanence (jamais transmis au navigateur).
    auto_refresh_token=False et persist_session=False : sans ça, le client essaie de
    gérer une session utilisateur classique, ce qui peut casser l'en-tête
    d'autorisation envoyé aux fonctions d'administration (auth.admin.*)."""
    url = st.secrets["SUPABASE_URL"].strip()
    key = st.secrets["SUPABASE_SERVICE_KEY"].strip().replace("\n", "").replace(" ", "")
    return create_client(
        url,
        key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )


# ============================================================
# AUTHENTIFICATION & CONTRÔLE D'ACCÈS
# ============================================================

def login(email: str, password: str):
    supabase = get_supabase_client()
    try:
        auth_response = supabase.auth.sign_in_with_password(
            {"email": email, "password": password}
        )
        user = auth_response.user
        if user is None:
            return False, "Identifiants incorrects."

        profil = (
            supabase.table("profils")
            .select("*")
            .eq("id", user.id)
            .single()
            .execute()
        )
        if not profil.data:
            return False, "Profil introuvable. Contactez le Directeur Général."
        if not profil.data.get("actif", True):
            return False, "Ce compte a été désactivé. Contactez le Directeur Général."

        st.session_state["user_id"] = user.id
        st.session_state["role"] = profil.data["role"]
        st.session_state["nom"] = profil.data["nom"]
        st.session_state["logged_in"] = True
        return True, "OK"
    except Exception as e:
        return False, f"Erreur de connexion : {e}"


def logout():
    supabase = get_supabase_client()
    try:
        supabase.auth.sign_out()
    except Exception:
        pass
    for key in ["user_id", "role", "nom", "logged_in"]:
        st.session_state.pop(key, None)


def is_logged_in() -> bool:
    return st.session_state.get("logged_in", False)


def _slugify(texte: str, max_len: int = 25) -> str:
    """Nettoie un texte pour un usage dans un identifiant/nom de fichier :
    retire les accents, remplace tout caractère non alphanumérique par '-',
    tronque à max_len caractères."""
    if not texte:
        return "x"
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")
    texte = re.sub(r"[^a-zA-Z0-9]+", "-", texte).strip("-")
    return (texte[:max_len].strip("-") or "x")


def generer_numero_commande(supabase, client_nom, support, hauteur, largeur, nb_exemplaires) -> str:
    """Génère un identifiant unique et lisible, ex :
    CMD-2026-000123_Jean-Dupont-Bache-Mate-2.00x1.50ml-x10
    Le compteur (000123) garantit l'unicité même si deux commandes ont
    exactement le même client/support/dimensions/quantité."""
    annee = datetime.now().year
    count_res = supabase.table("commandes").select("id", count="exact").execute()
    count = (count_res.count or 0) + 1

    client_slug = _slugify(client_nom)
    support_slug = _slugify(support)
    dimensions = f"{hauteur:.2f}x{largeur:.2f}ml"

    return f"CMD-{annee}-{count:06d}_{client_slug}-{support_slug}-{dimensions}-x{nb_exemplaires}"


@st.cache_data
def get_logo_base64():
    """Charge le logo GEMSTONE en base64 pour l'intégrer dans la bannière.
    Cherche successivement plusieurs emplacements possibles.
    Retourne None si introuvable (l'app fonctionne quand même, avec un titre texte)."""
    chemins_possibles = [
        "gemstone_logo.png",
        "assets/gemstone_logo.png",
    ]
    for chemin in chemins_possibles:
        try:
            with open(chemin, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except FileNotFoundError:
            continue
    return None


# ============================================================
# ÉCRAN DE CONNEXION
# ============================================================

def afficher_login():
    logo_b64 = get_logo_base64()
    if logo_b64:
        logo_html = f'<img src="data:image/png;base64,{logo_b64}" class="gemstone-logo" alt="GEMSTONE" />'
    else:
        logo_html = "<h1>💎 GEMSTONE</h1>"

    st.markdown(
        f"""
        <style>
        .gemstone-hero {{
            background: linear-gradient(135deg, #0f2027 0%, #203a43 45%, #2c5364 100%);
            border-radius: 18px;
            padding: 40px 32px;
            text-align: center;
            margin-bottom: 28px;
            color: #ffffff;
            box-shadow: 0 8px 24px rgba(0,0,0,0.15);
        }}
        .gemstone-logo {{
            max-width: 320px;
            width: 80%;
            height: auto;
            margin-bottom: 12px;
        }}
        .gemstone-hero h1 {{
            font-size: 2.6rem;
            margin-bottom: 6px;
            letter-spacing: 1px;
        }}
        .gemstone-hero p {{
            font-size: 1.1rem;
            opacity: 0.92;
            max-width: 560px;
            margin: 8px auto 0 auto;
        }}
        .gemstone-values {{
            display: flex;
            justify-content: center;
            gap: 20px;
            margin-top: 28px;
            flex-wrap: wrap;
        }}
        .gemstone-value {{
            background: rgba(255,255,255,0.10);
            border: 1px solid rgba(255,255,255,0.18);
            border-radius: 10px;
            padding: 10px 20px;
            font-size: 0.95rem;
            font-weight: 500;
        }}
        </style>
        <div class="gemstone-hero">
            {logo_html}
            <p>Chaque commande façonnée avec précision, chaque client servi avec fierté.
            La qualité de notre travail est la meilleure publicité de l'atelier.</p>
            <div class="gemstone-values">
                <div class="gemstone-value">🎯 Précision</div>
                <div class="gemstone-value">⚡ Réactivité</div>
                <div class="gemstone-value">🤝 Confiance</div>
                <div class="gemstone-value">✨ Excellence</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("Connexion")

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Mot de passe", type="password")
        submitted = st.form_submit_button("Se connecter")

        if submitted:
            if not email or not password:
                st.error("Veuillez renseigner votre email et votre mot de passe.")
            else:
                success, message = login(email, password)
                if success:
                    st.rerun()
                else:
                    st.error(message)

    st.caption("Ensemble, nous imprimons l'excellence — une commande à la fois. 💎")


# ============================================================
# ESPACE SECRÉTAIRE
# ============================================================

def afficher_espace_secretaire(supabase):
    st.title("📝 Espace Secrétaire")
    st.caption(f"Connecté en tant que {st.session_state['nom']}")

    tab_nouvelle, tab_liste = st.tabs(["➕ Nouvelle commande", "📋 Mes commandes saisies"])

    with tab_nouvelle:
        st.subheader("Saisie d'une nouvelle commande (reçue via WhatsApp)")

        machinistes_res = (
            supabase.table("profils")
            .select("id, nom")
            .eq("role", "machiniste")
            .eq("actif", True)
            .execute()
        )
        machinistes = machinistes_res.data or []

        if not machinistes:
            st.error("Aucun machiniste actif trouvé. Contactez le Directeur Général.")
        else:
            with st.form("nouvelle_commande", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    client_nom = st.text_input("Nom du client *")
                    client_contact = st.text_input("Contact WhatsApp *")
                    support = st.selectbox("Support d'impression *", SUPPORTS_IMPRESSION)
                with col2:
                    nb_exemplaires = st.number_input("Nombre d'exemplaires *", min_value=1, value=1)
                    hauteur = st.number_input("Hauteur (ml) *", min_value=0.0, step=0.01, format="%.2f")
                    largeur = st.number_input("Largeur (ml) *", min_value=0.0, step=0.01, format="%.2f")

                machiniste_choisi = st.selectbox(
                    "Assigner à un machiniste *",
                    options=[m["id"] for m in machinistes],
                    format_func=lambda mid: next(m["nom"] for m in machinistes if m["id"] == mid),
                )

                fichier = st.file_uploader(
                    "Fichier client (tous formats acceptés : PDF, JPG, PNG, TIFF, AI, PSD, EPS, CDR...)"
                )

                submitted = st.form_submit_button("✅ Valider la commande")

                if submitted:
                    if not client_nom or not client_contact or hauteur <= 0 or largeur <= 0:
                        st.error("Veuillez remplir tous les champs obligatoires (*).")
                    else:
                        try:
                            client_res = (
                                supabase.table("clients")
                                .select("id")
                                .eq("contact_whatsapp", client_contact)
                                .execute()
                            )
                            if client_res.data:
                                client_id = client_res.data[0]["id"]
                            else:
                                new_client = (
                                    supabase.table("clients")
                                    .insert({"nom": client_nom, "contact_whatsapp": client_contact})
                                    .execute()
                                )
                                client_id = new_client.data[0]["id"]

                            numero_commande = generer_numero_commande(
                                supabase, client_nom, support, hauteur, largeur, int(nb_exemplaires)
                            )

                            fichier_path = None
                            if fichier is not None:
                                extension = fichier.name.split(".")[-1] if "." in fichier.name else "bin"
                                fichier_path = f"{numero_commande}.{extension}"
                                supabase.storage.from_("commandes-fichiers").upload(
                                    fichier_path, fichier.getvalue()
                                )

                            supabase.table("commandes").insert(
                                {
                                    "numero_commande": numero_commande,
                                    "date_heure_saisie": datetime.now().isoformat(),
                                    "client_id": client_id,
                                    "support_impression": support,
                                    "nombre_exemplaires": int(nb_exemplaires),
                                    "hauteur": hauteur,
                                    "largeur": largeur,
                                    "fichier_path": fichier_path,
                                    "secretaire_id": st.session_state["user_id"],
                                    "machiniste_id": machiniste_choisi,
                                    "statut": STATUT_EN_ATTENTE,
                                }
                            ).execute()

                            st.success(f"Commande **{numero_commande}** enregistrée et assignée avec succès !")
                        except Exception as e:
                            st.error(f"Erreur lors de l'enregistrement : {e}")

    with tab_liste:
        st.subheader("Commandes que vous avez saisies")
        if st.button("🔄 Actualiser", key="refresh_secretaire"):
            st.rerun()

        mes_commandes = (
            supabase.table("commandes")
            .select(
                "numero_commande, date_heure_saisie, statut, support_impression, "
                "nombre_exemplaires, clients(nom, contact_whatsapp)"
            )
            .eq("secretaire_id", st.session_state["user_id"])
            .order("date_heure_saisie", desc=True)
            .execute()
        )

        if mes_commandes.data:
            for cmd in mes_commandes.data:
                contact = cmd["clients"]["contact_whatsapp"] if cmd.get("clients") else "—"
                st.write(
                    f"**{cmd['numero_commande']}** — 📞 {contact} — "
                    f"{STATUT_LABELS.get(cmd['statut'], cmd['statut'])}"
                )
        else:
            st.info("Aucune commande saisie pour le moment.")


# ============================================================
# ESPACE MACHINISTE
# ============================================================

def afficher_espace_machiniste(supabase):
    st.title("🖨️ Espace Machiniste")
    st.caption(f"Connecté en tant que {st.session_state['nom']}")

    if st.button("🔄 Actualiser", key="refresh_machiniste"):
        st.rerun()

    commandes_res = (
        supabase.table("commandes")
        .select(
            "id, numero_commande, date_heure_saisie, statut, support_impression, "
            "nombre_exemplaires, hauteur, largeur, fichier_path, clients(nom, contact_whatsapp)"
        )
        .eq("machiniste_id", st.session_state["user_id"])
        .in_("statut", [STATUT_EN_ATTENTE, STATUT_EN_COURS])
        .order("date_heure_saisie", desc=False)
        .execute()
    )

    commandes = commandes_res.data or []

    if not commandes:
        st.info("Aucune commande assignée en attente pour le moment.")
        return

    for cmd in commandes:
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"### {cmd['numero_commande']}")
                st.write(
                    f"**Support :** {cmd['support_impression']} — "
                    f"**Exemplaires :** {cmd['nombre_exemplaires']}"
                )
                st.write(f"**Dimensions :** {cmd['hauteur']} x {cmd['largeur']} ml")
                st.write(f"**Statut actuel :** {STATUT_LABELS.get(cmd['statut'], cmd['statut'])}")

            with col2:
                if cmd["fichier_path"]:
                    try:
                        file_bytes = supabase.storage.from_("commandes-fichiers").download(
                            cmd["fichier_path"]
                        )
                        clicked = st.download_button(
                            "⬇️ Télécharger",
                            data=file_bytes,
                            file_name=cmd["fichier_path"],
                            key=f"dl_{cmd['id']}",
                        )
                        # La même action sert le fichier ET bascule le statut :
                        # le machiniste ne peut pas "oublier" de le faire.
                        if clicked and cmd["statut"] == STATUT_EN_ATTENTE:
                            supabase.table("commandes").update(
                                {
                                    "statut": STATUT_EN_COURS,
                                    "date_debut_impression": datetime.now().isoformat(),
                                }
                            ).eq("id", cmd["id"]).execute()
                            st.success("Statut mis à jour : en cours d'impression.")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Fichier introuvable : {e}")
                else:
                    st.warning("Aucun fichier joint à cette commande.")


# ============================================================
# ESPACE CAISSIÈRE
# ============================================================

def afficher_espace_caissiere(supabase):
    st.title("💰 Espace Caissière")
    st.caption(f"Connecté en tant que {st.session_state['nom']}")

    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=8000, key="caissiere_autorefresh")
    except ImportError:
        if st.button("🔄 Actualiser", key="refresh_caissiere"):
            st.rerun()

    tab_suivi, tab_historique = st.tabs(["📋 Commandes en cours", "📜 Historique des livraisons"])

    with tab_suivi:
        commandes_res = (
            supabase.table("commandes")
            .select(
                "id, numero_commande, statut, support_impression, nombre_exemplaires, "
                "clients(nom, contact_whatsapp), profils!machiniste_id(nom)"
            )
            .in_("statut", [STATUT_EN_ATTENTE, STATUT_EN_COURS])
            .order("date_heure_saisie", desc=False)
            .execute()
        )
        commandes = commandes_res.data or []

        if not commandes:
            st.info("Aucune commande en cours de traitement.")
        else:
            for cmd in commandes:
                client = cmd.get("clients") or {}
                machiniste = cmd.get("profils") or {}
                with st.container(border=True):
                    st.markdown(f"### {cmd['numero_commande']}")
                    st.write(f"📞 {client.get('contact_whatsapp', '—')}")
                    st.write(f"**Machiniste assigné :** {machiniste.get('nom', '—')}")
                    st.write(f"**Statut :** {STATUT_LABELS.get(cmd['statut'], cmd['statut'])}")

                    if cmd["statut"] == STATUT_EN_COURS:
                        st.success(
                            "Le machiniste a lancé l'impression — vous pouvez contacter "
                            "le client sur WhatsApp."
                        )
                        with st.form(f"livraison_{cmd['id']}"):
                            mode_paiement = st.radio(
                                "Mode de règlement",
                                ["momo", "especes"],
                                key=f"mode_{cmd['id']}",
                                format_func=lambda x: "MoMo" if x == "momo" else "Espèces",
                                horizontal=True,
                            )
                            montant = st.number_input(
                                "Montant total (FCFA)", min_value=0, key=f"montant_{cmd['id']}"
                            )
                            valider = st.form_submit_button("✅ Marquer Livrée et Payée")
                            if valider:
                                supabase.table("commandes").update(
                                    {
                                        "statut": STATUT_LIVREE,
                                        "date_livraison": datetime.now().isoformat(),
                                        "mode_paiement": mode_paiement,
                                        "montant_total": montant,
                                        "caissiere_id": st.session_state["user_id"],
                                    }
                                ).eq("id", cmd["id"]).execute()
                                st.success(f"Commande {cmd['numero_commande']} clôturée.")
                                st.rerun()
                    else:
                        st.caption("⏳ En attente que le machiniste télécharge le fichier.")

    with tab_historique:
        historique_res = (
            supabase.table("commandes")
            .select("numero_commande, date_livraison, montant_total, mode_paiement, clients(nom)")
            .eq("statut", STATUT_LIVREE)
            .order("date_livraison", desc=True)
            .limit(50)
            .execute()
        )
        historique = historique_res.data or []

        if historique:
            for h in historique:
                mode = "MoMo" if h["mode_paiement"] == "momo" else "Espèces"
                st.write(
                    f"**{h['numero_commande']}** — "
                    f"{h['montant_total']} FCFA ({mode}) — {h['date_livraison']}"
                )
        else:
            st.info("Aucune livraison enregistrée pour le moment.")


# ============================================================
# ESPACE DG — TABLEAU DE BORD
# ============================================================

def afficher_dashboard_dg(supabase):
    if st.button("🔄 Actualiser", key="refresh_dg_dashboard"):
        st.rerun()

    commandes_res = (
        supabase.table("commandes")
        .select(
            "id, numero_commande, statut, montant_total, mode_paiement, "
            "date_heure_saisie, date_livraison, profils!machiniste_id(nom)"
        )
        .execute()
    )
    commandes = commandes_res.data or []
    df = pd.DataFrame(commandes)

    if not df.empty:
        # utc=True + tz_localize(None) : uniformise toutes les dates en UTC sans fuseau,
        # sinon les comparaisons avec pd.Timestamp.now() (naïf) plantent avec un TypeError
        # car Supabase renvoie des dates avec fuseau horaire (+00:00).
        df["date_heure_saisie"] = (
            pd.to_datetime(df["date_heure_saisie"], errors="coerce", utc=True).dt.tz_localize(None)
        )
        df["date_livraison"] = (
            pd.to_datetime(df["date_livraison"], errors="coerce", utc=True).dt.tz_localize(None)
        )

    df_livrees = df[df["statut"] == STATUT_LIVREE].copy() if not df.empty else df

    # ------------------------------------------------------------
    # Indicateurs globaux (depuis le début)
    # ------------------------------------------------------------
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Commandes totales", len(df))
    with col2:
        en_cours = len(df[df["statut"] != STATUT_LIVREE]) if not df.empty else 0
        st.metric("En cours de traitement", en_cours)
    with col3:
        ca_total = df_livrees["montant_total"].sum() if not df_livrees.empty else 0
        st.metric("Chiffre d'affaires total", f"{ca_total:,.0f} FCFA")
    with col4:
        st.metric("Commandes livrées", len(df_livrees))

    st.divider()

    # ------------------------------------------------------------
    # Aujourd'hui vs Ce mois-ci
    # ------------------------------------------------------------
    st.subheader("📅 Performance journalière & mensuelle")

    aujourdhui = pd.Timestamp.now().normalize()
    debut_mois = aujourdhui.replace(day=1)
    debut_annee = aujourdhui.replace(month=1, day=1)

    if not df_livrees.empty:
        est_aujourdhui = df_livrees["date_livraison"].dt.normalize() == aujourdhui
        est_ce_mois = df_livrees["date_livraison"] >= debut_mois
        est_cette_annee = df_livrees["date_livraison"] >= debut_annee
        ca_jour = df_livrees.loc[est_aujourdhui, "montant_total"].sum()
        nb_jour = int(est_aujourdhui.sum())
        ca_mois = df_livrees.loc[est_ce_mois, "montant_total"].sum()
        nb_mois = int(est_ce_mois.sum())
        ca_annee = df_livrees.loc[est_cette_annee, "montant_total"].sum()
        nb_annee = int(est_cette_annee.sum())
    else:
        ca_jour = nb_jour = ca_mois = nb_mois = ca_annee = nb_annee = 0

    col_j, col_m, col_an = st.columns(3)
    with col_j:
        st.markdown("**Aujourd'hui**")
        st.metric("Chiffre d'affaires", f"{ca_jour:,.0f} FCFA")
        st.metric("Commandes livrées", nb_jour)
    with col_m:
        st.markdown(f"**Ce mois-ci ({debut_mois.strftime('%B %Y')})**")
        st.metric("Chiffre d'affaires", f"{ca_mois:,.0f} FCFA")
        st.metric("Commandes livrées", nb_mois)
    with col_an:
        st.markdown(f"**Cette année ({debut_annee.strftime('%Y')})**")
        st.metric("Chiffre d'affaires", f"{ca_annee:,.0f} FCFA")
        st.metric("Commandes livrées", nb_annee)

    st.divider()

    # ------------------------------------------------------------
    # Courbes d'évolution
    # ------------------------------------------------------------
    st.subheader("📈 Évolution du chiffre d'affaires")

    if not df_livrees.empty:
        tab_jour, tab_mois, tab_annee = st.tabs(
            ["Par jour (30 derniers jours)", "Par mois", "Par année"]
        )

        with tab_jour:
            debut_periode = aujourdhui - pd.Timedelta(days=29)
            df_periode = df_livrees[df_livrees["date_livraison"] >= debut_periode].copy()
            df_periode["jour"] = df_periode["date_livraison"].dt.date
            ca_par_jour = df_periode.groupby("jour")["montant_total"].sum()
            # Complète les jours sans vente à 0, pour une courbe continue sans trous
            toutes_les_dates = pd.date_range(debut_periode, aujourdhui, freq="D").date
            ca_par_jour = ca_par_jour.reindex(toutes_les_dates, fill_value=0)
            st.line_chart(ca_par_jour)

        with tab_mois:
            df_mensuel = df_livrees.copy()
            df_mensuel["mois"] = df_mensuel["date_livraison"].dt.to_period("M").astype(str)
            ca_par_mois = df_mensuel.groupby("mois")["montant_total"].sum()
            st.line_chart(ca_par_mois)

        with tab_annee:
            df_annuel = df_livrees.copy()
            df_annuel["annee"] = df_annuel["date_livraison"].dt.year
            ca_par_annee = df_annuel.groupby("annee")["montant_total"].sum()
            st.bar_chart(ca_par_annee)
    else:
        st.info("Pas encore de commande livrée — la courbe apparaîtra dès la première vente.")

    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("💳 Répartition des paiements")
        if not df_livrees.empty:
            paiements = df_livrees.groupby("mode_paiement")["montant_total"].sum()
            st.bar_chart(paiements)
        else:
            st.info("Aucun paiement enregistré pour le moment.")

    with col_b:
        st.subheader("👷 Volume de commandes par machiniste")
        if not df.empty:
            df["machiniste_nom"] = df["profils"].apply(
                lambda x: x["nom"] if isinstance(x, dict) else "—"
            )
            perf = df.groupby("machiniste_nom")["id"].count()
            st.bar_chart(perf)
        else:
            st.info("Aucune donnée disponible.")

    st.divider()
    st.subheader("📋 Toutes les commandes")
    if not df.empty:
        st.dataframe(
            df[
                [
                    "numero_commande",
                    "statut",
                    "montant_total",
                    "mode_paiement",
                    "date_heure_saisie",
                    "date_livraison",
                ]
            ],
            use_container_width=True,
        )
    else:
        st.info("Aucune commande enregistrée pour le moment.")


# ============================================================
# ESPACE DG — GESTION DES EMPLOYÉS
# ============================================================

def _creer_employe(supabase, supabase_admin, nom, email, password, role):
    try:
        result = supabase_admin.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True}
        )
        supabase.table("profils").insert(
            {
                "id": result.user.id,
                "nom": nom,
                "email": email,
                "role": role,
                "actif": True,
            }
        ).execute()
        st.success(f"Profil de **{nom}** ({ROLE_LABELS[role]}) créé avec succès.")
        st.rerun()
    except Exception as e:
        st.error(f"Erreur lors de la création : {e}")


def afficher_gestion_employes(supabase, supabase_admin):
    st.subheader("➕ Créer un nouvel employé")

    with st.form("nouvel_employe", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            nom = st.text_input("Nom complet *")
            email = st.text_input("Email de connexion *")
        with col2:
            role = st.selectbox(
                "Rôle *",
                ROLES_GERES_PAR_DG,
                format_func=lambda r: ROLE_LABELS[r],
            )
            password = st.text_input("Mot de passe temporaire *", type="password")

        submitted = st.form_submit_button("✅ Créer le profil")

        if submitted:
            if not nom or not email or not password:
                st.error("Tous les champs sont obligatoires.")
            elif len(password) < 6:
                st.error("Le mot de passe doit contenir au moins 6 caractères.")
            elif role == "caissiere":
                caissiere_existante = (
                    supabase.table("profils")
                    .select("id, nom")
                    .eq("role", "caissiere")
                    .eq("actif", True)
                    .execute()
                )
                if caissiere_existante.data:
                    nom_actuelle = caissiere_existante.data[0]["nom"]
                    st.error(
                        f"Un profil Caissière actif existe déjà ({nom_actuelle}). "
                        f"Supprimez-le ou désactivez-le ci-dessous avant d'en créer un nouveau."
                    )
                else:
                    _creer_employe(supabase, supabase_admin, nom, email, password, role)
            else:
                _creer_employe(supabase, supabase_admin, nom, email, password, role)

    st.divider()
    st.subheader("📋 Employés existants")

    employes_res = (
        supabase.table("profils")
        .select("id, nom, email, role, actif")
        .in_("role", ROLES_GERES_PAR_DG)
        .order("role")
        .order("nom")
        .execute()
    )
    employes = employes_res.data or []

    if not employes:
        st.info("Aucun employé créé pour le moment.")
        return

    for emp in employes:
        with st.container(border=True):
            col1, col2, col3, col4 = st.columns([3, 2, 2, 2])
            with col1:
                statut_icone = "🟢" if emp["actif"] else "⚪"
                st.write(f"{statut_icone} **{emp['nom']}**")
                st.caption(emp.get("email") or "—")
            with col2:
                st.write(ROLE_LABELS.get(emp["role"], emp["role"]))
            with col3:
                label = "Désactiver" if emp["actif"] else "Réactiver"
                if st.button(label, key=f"toggle_{emp['id']}"):
                    supabase.table("profils").update({"actif": not emp["actif"]}).eq(
                        "id", emp["id"]
                    ).execute()
                    st.rerun()
            with col4:
                if st.button("🗑️ Supprimer", key=f"del_{emp['id']}"):
                    try:
                        # La suppression du compte auth entraîne la suppression du profil
                        # (ON DELETE CASCADE). Si l'employé a des commandes à son nom,
                        # la base refuse la suppression pour préserver l'historique —
                        # dans ce cas, on désactive le compte à la place.
                        supabase_admin.auth.admin.delete_user(emp["id"])
                        st.success(f"{emp['nom']} supprimé définitivement.")
                    except Exception:
                        supabase.table("profils").update({"actif": False}).eq(
                            "id", emp["id"]
                        ).execute()
                        st.warning(
                            f"{emp['nom']} a des commandes associées : le compte a été "
                            f"désactivé au lieu d'être supprimé, pour préserver l'historique."
                        )
                    st.rerun()


def afficher_espace_dg(supabase, supabase_admin):
    st.title("📊 Espace Direction Générale")

    tab_dashboard, tab_employes = st.tabs(["📊 Tableau de bord", "👥 Gestion des employés"])

    with tab_dashboard:
        afficher_dashboard_dg(supabase)

    with tab_employes:
        afficher_gestion_employes(supabase, supabase_admin)


# ============================================================
# ROUTAGE PRINCIPAL
# ============================================================

def main():
    if not is_logged_in():
        afficher_login()
        return

    supabase = get_supabase_client()
    role = st.session_state.get("role")

    with st.sidebar:
        st.write(f"👤 **{st.session_state['nom']}**")
        st.write(f"Rôle : `{role}`")
        if st.button("🚪 Se déconnecter"):
            logout()
            st.rerun()

    if role == "secretaire":
        afficher_espace_secretaire(supabase)
    elif role == "machiniste":
        afficher_espace_machiniste(supabase)
    elif role == "caissiere":
        afficher_espace_caissiere(supabase)
    elif role == "dg":
        supabase_admin = get_supabase_admin_client()
        afficher_espace_dg(supabase, supabase_admin)
    else:
        st.error("Rôle non reconnu. Contactez le Directeur Général.")


if __name__ == "__main__":
    main()
