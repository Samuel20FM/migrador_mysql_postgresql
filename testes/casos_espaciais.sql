-- Colunas espaciais (precisa de PostGIS no destino).
DROP DATABASE IF EXISTS espaciais;
CREATE DATABASE espaciais CHARACTER SET utf8mb4;
USE espaciais;
CREATE TABLE lugar (id int AUTO_INCREMENT PRIMARY KEY, nome varchar(40) NOT NULL,
  ponto point NOT NULL SRID 4326, area polygon NULL, qualquer geometry NULL, SPATIAL KEY sp_ponto (ponto));
INSERT INTO lugar (nome, ponto, area, qualquer) VALUES
 ('Maputo', ST_GeomFromText('POINT(-25.9692 32.5732)', 4326), ST_GeomFromText('POLYGON((0 0,10 0,10 10,0 10,0 0))'), ST_GeomFromText('LINESTRING(0 0,1 1,2 3)')),
 ('Beira', ST_GeomFromText('POINT(-19.8436 34.8389)', 4326), NULL, NULL);
