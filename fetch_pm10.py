"""Scraper des mesures PM10 en direct de Wallon'Air.

Ce script :
1. Télécharge la page des mesures en direct (requests).
2. Extrait les valeurs PM10 de chaque station (BeautifulSoup).
3. Sauvegarde les données en DOUBLE :
   - dans un fichier JSON quotidien   -> data/YYYY-MM-DD.json
   - dans une base SQLite             -> data/wallonair.db

================================================================================
POINT PÉDAGOGIQUE : deux philosophies de stockage très différentes
================================================================================
JSON (fichier texte) :
    On ne peut pas "ajouter une ligne" à un fichier JSON existant.
    Le cycle est toujours : CHARGER tout le fichier en mémoire (dict Python)
    -> MODIFIER le dict -> RÉÉCRIRE le fichier EN ENTIER.
    C'est simple et lisible par un humain, mais tout passe par la mémoire.

SQLite (base de données) :
    On travaille de façon incrémentale : OUVRIR une connexion
    -> EXÉCUTER du SQL (INSERT...) -> COMMIT (valider la transaction)
    -> FERMER la connexion.
    La base n'est jamais réécrite en entier : SQLite ajoute les nouvelles
    lignes directement dans le fichier .db, de façon transactionnelle
    (si le script plante avant le commit, rien n'est écrit : pas de corruption).
================================================================================
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Constantes du projet ----------------------------------------------------
URL_MESURES = "https://www.wallonair.be/fr/mesures/mesures-en-direct?view=mesures"

# Certains sites refusent les requêtes sans "User-Agent" (ils pensent à un robot).
# On s'identifie donc comme un navigateur classique.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Path(__file__).parent = dossier où se trouve ce script.
# Ainsi le dossier data/ est toujours au bon endroit, peu importe d'où
# on lance le script (important pour GitHub Actions).
DOSSIER_DATA = Path(__file__).parent / "data"
CHEMIN_DB = DOSSIER_DATA / "wallonair.db"


def recuperer_page() -> str:
    """Télécharge le HTML de la page des mesures.

    Si le site est inaccessible (panne, timeout...), requests lève une
    exception qui REMONTE jusqu'à main() : le script s'arrête AVANT
    d'avoir touché aux fichiers existants -> aucune corruption possible.
    """
    print(f"Téléchargement de {URL_MESURES} ...")
    reponse = requests.get(URL_MESURES, headers=HEADERS, timeout=30)

    # raise_for_status() lève une exception si le serveur répond une erreur
    # (404, 500...). Sans cet appel, on continuerait avec une page invalide.
    reponse.raise_for_status()
    return reponse.text


def extraire_mesures(html: str) -> tuple[str, list[dict]] | None:
    """Extrait l'horodatage officiel et les mesures PM10 du HTML.

    Retourne (timestamp, mesures) ou None si le site répond mais n'a pas
    encore publié de valeurs PM10 (maintenance nocturne, page vide...).
    Chaque mesure est un dict : {"station_name": ..., "pm10_value": ...}.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- 1) L'horodatage officiel de la page ---------------------------------
    # La page affiche "Dernière observation disponible : (09/06/2026 19:30)".
    # On utilise CET horodatage plutôt que l'heure d'exécution du script,
    # car le cron GitHub peut tourner avec du retard.
    # L'expression régulière capture "JJ/MM/AAAA HH:MM".
    correspondance = re.search(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}:\d{2})", html)
    if correspondance is None:
        raise ValueError("Horodatage introuvable : la page a peut-être changé de format.")

    jour, mois, annee, heure = correspondance.groups()
    # On reformate en "AAAA-MM-JJ HH:MM" : ce format se trie naturellement
    # par ordre chronologique (pratique en SQL comme en JSON).
    timestamp = f"{annee}-{mois}-{jour} {heure}"

    # --- 2) Le tableau des mesures en direct ---------------------------------
    # La page contient 2 tableaux : le 1er = mesures en direct (celui qu'on veut),
    # le 2e = dernières valeurs validées.
    tableau = soup.find("table")
    if tableau is None:
        raise ValueError("Tableau des mesures introuvable : la page a peut-être changé.")

    mesures: list[dict] = []
    for ligne in tableau.find_all("tr"):
        cellules = ligne.find_all("td")

        # Lignes à ignorer :
        # - la ligne d'en-tête (que des <th>, donc 0 <td>)
        # - les lignes "Province de ..." (une seule cellule, classe "provinces")
        if len(cellules) < 2 or "provinces" in cellules[0].get("class", []):
            continue

        station = cellules[0].get_text(strip=True)
        # La colonne PM10 est la 2e du tableau (juste après le nom de station).
        texte_pm10 = cellules[1].get_text(strip=True)

        # Certaines stations ne mesurent pas le PM10 (cellule vide ou "n/a").
        if texte_pm10 == "" or texte_pm10.lower() == "n/a":
            continue

        try:
            valeur_pm10 = float(texte_pm10)
        except ValueError:
            # Valeur inattendue : on la signale mais on ne plante pas tout.
            print(f"  ATTENTION : valeur PM10 illisible pour {station} : '{texte_pm10}'")
            continue

        mesures.append({"station_name": station, "pm10_value": valeur_pm10})

    if not mesures:
        # Pas une erreur fatale : Wallon'Air vide parfois son tableau vers 01h UTC
        # (maintenance). On signale et on laisse main() sortir proprement (code 0)
        # pour ne pas déclencher d'email d'échec GitHub Actions.
        print(
            f"Aucune mesure PM10 disponible pour l'observation du {timestamp} "
            "(page vide ou maintenance ?). Rien à enregistrer."
        )
        return None

    print(f"{len(mesures)} mesures PM10 extraites (observation du {timestamp}).")
    return timestamp, mesures


def sauvegarder_json(timestamp: str, mesures: list[dict]) -> bool:
    """Ajoute les mesures dans le fichier JSON du jour, sans doublon.

    Retourne True si le fichier a été modifié, False si l'heure existait déjà.

    Structure du fichier data/YYYY-MM-DD.json :
        {
            "2026-06-09 18:30": [{"station_name": "...", "pm10_value": ...}, ...],
            "2026-06-09 19:30": [...]
        }
    Utiliser le timestamp comme CLÉ du dictionnaire rend l'anti-doublon
    trivial : si la clé existe déjà, on ne fait rien.
    """
    # Le nom du fichier vient de la DATE de l'observation (10 premiers caractères).
    chemin_json = DOSSIER_DATA / f"{timestamp[:10]}.json"

    # ÉTAPE 1 : CHARGER. On lit tout le fichier existant en mémoire.
    # S'il n'existe pas encore (1re exécution du jour), on part d'un dict vide.
    if chemin_json.exists():
        with open(chemin_json, encoding="utf-8") as fichier:
            donnees = json.load(fichier)
    else:
        donnees = {}

    # Anti-doublon : si cette heure d'observation est déjà enregistrée, on sort.
    if timestamp in donnees:
        print(f"JSON : l'observation {timestamp} existe déjà dans {chemin_json.name}, rien à faire.")
        return False

    # ÉTAPE 2 : MODIFIER. On ajoute la nouvelle entrée au dict en mémoire.
    donnees[timestamp] = mesures

    # ÉTAPE 3 : RÉÉCRIRE. On écrase le fichier avec le contenu complet mis à jour.
    # indent=4 -> fichier lisible par un humain (et joli sur GitHub).
    # ensure_ascii=False -> garde les accents tels quels (é au lieu de \u00e9).
    with open(chemin_json, "w", encoding="utf-8") as fichier:
        json.dump(donnees, fichier, indent=4, ensure_ascii=False)

    print(f"JSON : observation {timestamp} ajoutée dans {chemin_json.name}.")
    return True


def sauvegarder_sqlite(timestamp: str, mesures: list[dict]) -> int:
    """Insère les mesures dans la base SQLite, sans doublon.

    Retourne le nombre de lignes réellement insérées.
    """
    # ÉTAPE 1 : OUVRIR LA CONNEXION.
    # sqlite3.connect() crée le fichier .db automatiquement s'il n'existe pas.
    connexion = sqlite3.connect(CHEMIN_DB)
    try:
        curseur = connexion.cursor()

        # ÉTAPE 2 : EXÉCUTER LE SQL.
        # "IF NOT EXISTS" -> la création n'a lieu qu'à la toute 1re exécution.
        # La contrainte UNIQUE(station_name, timestamp) interdit physiquement
        # d'avoir deux fois la même station à la même heure : c'est la base
        # elle-même qui garantit l'absence de doublons, pas notre code Python.
        curseur.execute(
            """
            CREATE TABLE IF NOT EXISTS mesures (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                station_name TEXT NOT NULL,
                pm10_value   REAL NOT NULL,
                timestamp    TEXT NOT NULL,
                UNIQUE (station_name, timestamp)
            )
            """
        )

        # "INSERT OR IGNORE" : si une ligne viole la contrainte UNIQUE
        # (= doublon), SQLite l'ignore silencieusement au lieu de planter.
        # executemany() répète la requête pour chaque mesure de la liste.
        # Les "?" sont des paramètres sécurisés (jamais de f-string dans du SQL !).
        curseur.executemany(
            "INSERT OR IGNORE INTO mesures (station_name, pm10_value, timestamp) VALUES (?, ?, ?)",
            [(m["station_name"], m["pm10_value"], timestamp) for m in mesures],
        )
        nb_inserees = connexion.total_changes

        # ÉTAPE 3 : COMMIT. Tant que commit() n'est pas appelé, les INSERT
        # sont "en attente" dans une transaction : rien n'est écrit sur le
        # disque. Un plantage avant le commit = base intacte (zéro corruption).
        connexion.commit()
    finally:
        # ÉTAPE 4 : FERMER. Le bloc "finally" garantit la fermeture de la
        # connexion même si une erreur survient au milieu.
        connexion.close()

    print(f"SQLite : {nb_inserees} nouvelle(s) ligne(s) insérée(s) dans {CHEMIN_DB.name}.")
    return nb_inserees


def main() -> None:
    """Orchestre le scraping puis le double stockage JSON + SQLite."""
    # Crée le dossier data/ s'il n'existe pas (1re exécution).
    DOSSIER_DATA.mkdir(exist_ok=True)

    # Toute exception ici (site en panne, page modifiée...) interrompt le
    # script AVANT les sauvegardes : les fichiers existants restent intacts,
    # et GitHub Actions marquera le job en erreur (visible dans l'onglet Actions).
    html = recuperer_page()
    resultat = extraire_mesures(html)
    if resultat is None:
        # Sortie propre : le job GitHub Actions reste vert (pas d'email d'échec).
        sys.exit(0)

    timestamp, mesures = resultat
    sauvegarder_json(timestamp, mesures)
    sauvegarder_sqlite(timestamp, mesures)

    print("Terminé.")


if __name__ == "__main__":
    main()
