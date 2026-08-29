"""
GEMSTONE — Gestion des commandes (Atelier d'impression)
Application Streamlit unique (tout-en-un) connectée à Supabase.

Rôles gérés dans ce même fichier :
- secretaire : saisie des commandes reçues via WhatsApp
- machiniste  : téléchargement du fichier + bascule automatique du statut
- caissiere   : suivi du statut + clôture livraison/paiement
- dg          : tableau de bord global temps réel

Prérequis Supabase : tables 'profils', 'clients', 'commandes'
                      + bucket Storage 'commandes-fichiers'
                      (voir supabase_schema.sql pour la création complète).
"""

from datetime import datetime

import pandas as pd
import streamlit as st
from supabase import create_client, Client

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


# ============================================================
# CONNEXION SUPABASE
# ============================================================

@st.cache_resource
def get_supabase_client() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


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


def generer_numero_commande(supabase) -> str:
    annee = datetime.now().year
    count_res = supabase.table("commandes").select("id", count="exact").execute()
    count = (count_res.count or 0) + 1
    return f"CMD-{annee}-{count:06d}"


# ============================================================
# ÉCRAN DE CONNEXION
# ============================================================

def afficher_login():
    st.title("💎 GEMSTONE — Gestion des commandes")
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

    st.caption(
        "Les comptes (Secrétaires, Machinistes, Caissière, DG) sont créés "
        "par le Directeur Général dans Supabase."
    )


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
                    support = st.selectbox(
                        "Support d'impression *",
                        ["Bâche", "Vinyle", "Papier photo", "Autocollant", "Autre"],
                    )
                with col2:
                    nb_exemplaires = st.number_input("Nombre d'exemplaires *", min_value=1, value=1)
                    hauteur = st.number_input("Hauteur (cm) *", min_value=0.0, step=0.1)
                    largeur = st.number_input("Largeur (cm) *", min_value=0.0, step=0.1)

                machiniste_choisi = st.selectbox(
                    "Assigner à un machiniste *",
                    options=[m["id"] for m in machinistes],
                    format_func=lambda mid: next(m["nom"] for m in machinistes if m["id"] == mid),
                )

                fichier = st.file_uploader(
                    "Fichier client (PDF, image...)", type=["pdf", "png", "jpg", "jpeg"]
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

                            numero_commande = generer_numero_commande(supabase)

                            fichier_path = None
                            if fichier is not None:
                                extension = fichier.name.split(".")[-1]
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
                "nombre_exemplaires, clients(nom)"
            )
            .eq("secretaire_id", st.session_state["user_id"])
            .order("date_heure_saisie", desc=True)
            .execute()
        )

        if mes_commandes.data:
            for cmd in mes_commandes.data:
                client_nom = cmd["clients"]["nom"] if cmd.get("clients") else "—"
                st.write(
                    f"**{cmd['numero_commande']}** — {client_nom} — "
                    f"{cmd['support_impression']} x{cmd['nombre_exemplaires']} — "
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
        client_nom = cmd["clients"]["nom"] if cmd.get("clients") else "—"
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"### {cmd['numero_commande']} — {client_nom}")
                st.write(
                    f"**Support :** {cmd['support_impression']} — "
                    f"**Exemplaires :** {cmd['nombre_exemplaires']}"
                )
                st.write(f"**Dimensions :** {cmd['hauteur']} x {cmd['largeur']} cm")
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
                    st.markdown(f"### {cmd['numero_commande']} — {client.get('nom', '—')}")
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
                client_nom = h["clients"]["nom"] if h.get("clients") else "—"
                mode = "MoMo" if h["mode_paiement"] == "momo" else "Espèces"
                st.write(
                    f"**{h['numero_commande']}** — {client_nom} — "
                    f"{h['montant_total']} FCFA ({mode}) — {h['date_livraison']}"
                )
        else:
            st.info("Aucune livraison enregistrée pour le moment.")


# ============================================================
# ESPACE DG
# ============================================================

def afficher_espace_dg(supabase):
    st.title("📊 Tableau de bord — Direction Générale")

    if st.button("🔄 Actualiser", key="refresh_dg"):
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

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Commandes totales", len(df))
    with col2:
        en_cours = len(df[df["statut"] != STATUT_LIVREE]) if not df.empty else 0
        st.metric("En cours de traitement", en_cours)
    with col3:
        ca_total = df[df["statut"] == STATUT_LIVREE]["montant_total"].sum() if not df.empty else 0
        st.metric("Chiffre d'affaires total", f"{ca_total:,.0f} FCFA")
    with col4:
        livrees = len(df[df["statut"] == STATUT_LIVREE]) if not df.empty else 0
        st.metric("Commandes livrées", livrees)

    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("💳 Répartition des paiements")
        if not df.empty and (df["statut"] == STATUT_LIVREE).any():
            paiements = df[df["statut"] == STATUT_LIVREE].groupby("mode_paiement")["montant_total"].sum()
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
        afficher_espace_dg(supabase)
    else:
        st.error("Rôle non reconnu. Contactez le Directeur Général.")


if __name__ == "__main__":
    main()
