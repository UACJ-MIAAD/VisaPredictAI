"""VisaPredict AI — capa de modelado (Proyecto de Innovación Tecnológica I).

El modelado consume EXCLUSIVAMENTE el almacén estrella (DuckDB) vía las vistas
``mart_training_F`` y ``mart_series_summary``. Nunca lee CSV crudo: el régimen de
entrenamiento (solo observaciones con estado e=F) ya está garantizado en SQL.
"""
