# github-action-pm10

Projet d'entraînement GitHub Actions : scraping horaire des mesures **PM10 en Wallonie**
depuis [Wallon'Air](https://www.wallonair.be/fr/mesures/mesures-en-direct?view=mesures),
avec un **double stockage** (approche GitOps hybride) :

- un fichier **JSON quotidien** : `data/YYYY-MM-DD.json` (une clé par heure d'observation) ;
- une base **SQLite** : `data/wallonair.db` (table `mesures`, anti-doublon par contrainte `UNIQUE`).

## Fonctionnement

1. Toutes les heures (cron `5 * * * *`), GitHub Actions lance `fetch_pm10.py` sur un runner Ubuntu.
2. Le script scrape le tableau des mesures en direct, puis écrit dans le JSON du jour **et** dans la base SQLite.
3. Le workflow commite et pousse les fichiers modifiés avec l'identité du bot `github-actions[bot]`.

Pour tester sans attendre le cron : onglet **Actions** → workflow "Scraping PM10 Wallonie" → bouton **Run workflow**.

## Lancer en local

```bash
pip install -r requirements.txt
python fetch_pm10.py
```

## Comparaison post-exécution : comment vérifier mes données ?

### Côté JSON (vérification visuelle, directement sur GitHub)

C'est le grand avantage du JSON : **GitHub sait l'afficher**.

1. Sur la page du dépôt, ouvrir le dossier `data/` puis cliquer sur un fichier `YYYY-MM-DD.json` :
   GitHub affiche le contenu formaté et colorisé, sans rien télécharger.
2. Cliquer sur **History** (en haut à droite du fichier) pour voir tous les commits du bot :
   chaque exécution horaire apparaît, et la vue "diff" montre exactement les lignes ajoutées
   (la nouvelle heure d'observation en vert).
3. Bonus : l'URL "Raw" du fichier peut être lue directement par un script ou un notebook
   (`pd.read_json(url)` par exemple).

### Côté SQLite (lecture en local, après récupération)

GitHub ne sait **pas** afficher un fichier `.db` (binaire) : il faut le récupérer en local.

```bash
git pull   # récupère la dernière version de data/wallonair.db
```

Puis, au choix :

- **DB Browser for SQLite** (interface graphique gratuite, [sqlitebrowser.org](https://sqlitebrowser.org)) :
  ouvrir `data/wallonair.db` → onglet "Browse Data" ou "Execute SQL".

- **Python pur** (module `sqlite3` inclus dans la bibliothèque standard) :

```python
import sqlite3

connexion = sqlite3.connect("data/wallonair.db")
for ligne in connexion.execute(
    "SELECT timestamp, station_name, pm10_value FROM mesures ORDER BY timestamp DESC LIMIT 10"
):
    print(ligne)
connexion.close()
```

- **Pandas** (idéal pour l'analyse de données) :

```python
import pandas as pd
import sqlite3

connexion = sqlite3.connect("data/wallonair.db")
# read_sql charge le résultat de la requête directement dans un DataFrame
df = pd.read_sql("SELECT * FROM mesures", connexion)
connexion.close()

# Exemple : moyenne de PM10 par station
print(df.groupby("station_name")["pm10_value"].mean().sort_values(ascending=False))
```

### En résumé

| | JSON quotidien | SQLite |
|---|---|---|
| Lisible sur GitHub | Oui (formaté + diff par commit) | Non (fichier binaire) |
| Requêtes / filtres | Manuels (boucles Python) | SQL (`WHERE`, `GROUP BY`...) |
| Diff Git | Léger (lignes ajoutées) | Fichier complet à chaque commit |
| Usage idéal | Inspection rapide, partage | Analyse, agrégations, historique long |

> Note : committer un fichier `.db` binaire dans Git n'est pas une pratique de production
> (le dépôt grossit à chaque commit car Git ne peut pas faire de diff binaire efficace),
> mais c'est parfaitement adapté à cet exercice d'apprentissage GitOps.
