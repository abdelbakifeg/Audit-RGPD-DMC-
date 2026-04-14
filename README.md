# Audit RGPD DMC — Déploiement Render

## Étapes de déploiement

### 1. Créer le repo GitHub

1. Va sur github.com → New repository
2. Nomme-le `audit-rgpd-dmc`
3. Mets-le en **Private**
4. Clone-le sur ton PC ou utilise l'interface web GitHub

### 2. Uploader les fichiers

Dépose tous ces fichiers dans le repo GitHub :

```
audit-rgpd-dmc/
├── modules/
│   ├── __init__.py              (fichier vide)
│   ├── cmp_detector.py
│   ├── didomi_extractor.py
│   ├── playwright_navigator.py  (version cloud)
│   ├── orchestrator.py
│   ├── cnil_analyzer.py
│   ├── report_generator_v2.py
│   └── api.py
├── static/
│   └── index.html
├── requirements.txt
├── render.yaml
└── .gitignore
```

### 3. Déployer sur Render

1. Va sur render.com → New → Web Service
2. Connecte ton compte GitHub
3. Sélectionne le repo `audit-rgpd-dmc`
4. Render détecte automatiquement `render.yaml`
5. Clique **Deploy**

### 4. Variables d'environnement sur Render

Dans le dashboard Render → Environment :

| Variable | Valeur | Obligatoire |
|---|---|---|
| `BROWSERLESS_TOKEN` | Ton token browserless.io | Pour les sites JS |
| `GEMINI_API_KEY` | Ta clé Gemini | Pour l'analyse IA |

**Browserless gratuit** : inscris-toi sur browserless.io → 400 minutes/mois gratuites

### 5. Accéder à l'outil

Une fois déployé Render te donne une URL du type :
```
https://audit-rgpd-dmc.onrender.com
```

C'est cette URL que toute l'équipe peut utiliser depuis n'importe quel navigateur.

---

## Notes importantes

- Le plan gratuit Render met le service en veille après 15 minutes d'inactivité
- Premier chargement après veille : ~30 secondes
- Pour éviter la veille : passer au plan Starter (7$/mois)
- Les fichiers générés (PDF, Excel) sont stockés temporairement — ils sont perdus au redémarrage du service
- Pour conserver les rapports : connecter Google Drive ou S3 (évolution future)
