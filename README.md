# Atelier Impression — Gestion des commandes

Application Streamlit connectée à Supabase pour digitaliser le workflow de commandes d'un atelier d'impression (Secrétaire → Machiniste → Caissière → DG).

## Structure

```
atelier-impression/
├── app.py                     # Point d'entrée : connexion + redirection par rôle
├── pages/
│   ├── 1_Secretaire.py        # Saisie des commandes reçues via WhatsApp
│   ├── 2_Machiniste.py        # Commandes assignées + bascule auto du statut
│   ├── 3_Caissiere.py         # Suivi + clôture livraison/paiement
│   └── 4_DG.py                # Tableau de bord global
├── utils/
│   ├── supabase_client.py     # Connexion Supabase (cache Streamlit)
│   ├── auth.py                # Login + contrôle d'accès par rôle
│   └── statuts.py             # Constantes des statuts de commande
├── requirements.txt
└── .streamlit/secrets.toml.example
```

## Prérequis côté Supabase

Avant de lancer l'app, créer dans Supabase :

1. **Table `profils`** (liée à `auth.users`) : `id`, `nom`, `role` (`secretaire` / `machiniste` / `caissiere` / `dg`), `actif` (bool).
2. **Table `clients`** : `id`, `nom`, `contact_whatsapp`.
3. **Table `commandes`** : `id`, `numero_commande`, `date_heure_saisie`, `client_id`, `support_impression`, `nombre_exemplaires`, `hauteur`, `largeur`, `fichier_path`, `secretaire_id`, `machiniste_id`, `statut`, `date_debut_impression`, `date_livraison`, `mode_paiement`, `montant_total`, `caissiere_id`.
4. **Bucket Storage** nommé `commandes-fichiers`.
5. Un utilisateur Supabase Auth (email/mot de passe) par personne, avec une ligne correspondante dans `profils`.
6. **RLS activé** avec des policies filtrant selon `role` et `auth.uid()`.

## Installation locale

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# éditer .streamlit/secrets.toml avec vos identifiants Supabase
streamlit run app.py
```

## Test rapide dans Google Colab

Utiliser Colab pour valider les requêtes `supabase-py` (insertion, changement de statut, agrégations) avant de les intégrer dans les pages Streamlit, via :

```python
!pip install supabase
from supabase import create_client
supabase = create_client("SUPABASE_URL", "SUPABASE_KEY")
```

## Déploiement sur Streamlit Cloud

1. Pousser ce dossier sur un repo GitHub.
2. Sur [share.streamlit.io](https://share.streamlit.io), connecter le repo et sélectionner `app.py`.
3. Dans **Settings > Secrets**, coller le contenu de `secrets.toml.example` avec vos vraies valeurs.
4. Chaque push sur GitHub redéploie automatiquement.
