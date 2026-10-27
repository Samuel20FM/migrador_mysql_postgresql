-- Base de testes com os casos difíceis para o migrador.
SET SESSION sql_mode='';
DROP DATABASE IF EXISTS casos;
CREATE DATABASE casos CHARACTER SET utf8mb4;
USE casos;
CREATE TABLE pais (codigo varchar(10) PRIMARY KEY, nome varchar(50) NOT NULL, criado datetime NOT NULL DEFAULT '0000-00-00 00:00:00');
CREATE TABLE cidade (id int unsigned AUTO_INCREMENT PRIMARY KEY, pais varchar(10), nome varchar(80) NOT NULL,
  populacao bigint unsigned, ativa tinyint(1) DEFAULT 1, nivel tinyint(1) DEFAULT 0, area decimal(12,3), dens float, ratio double,
  fundada date, abre time, duracao time, tipo enum('a','b') NOT NULL DEFAULT 'a', tags set('x','y','z'),
  info json, foto blob, flag bit(1) DEFAULT b'1', mascara bit(8), ano year, nota text,
  atualizado timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_nome (nome(20)), FULLTEXT KEY ft_nota (nota),
  CONSTRAINT fk_cid_pais FOREIGN KEY (pais) REFERENCES pais(codigo)) ENGINE=InnoDB;
CREATE TABLE zona (pais varchar(10), num int, descricao varchar(40), KEY k_desc (descricao), PRIMARY KEY (pais, num));
CREATE TABLE ponto (id int AUTO_INCREMENT PRIMARY KEY, pais varchar(10), num int, ref_desc varchar(40),
  CONSTRAINT fk_ponto_zona FOREIGN KEY (pais, num) REFERENCES zona(pais, num),
  CONSTRAINT fk_ponto_desc FOREIGN KEY (ref_desc) REFERENCES zona(descricao));
CREATE TABLE sem_pk (a int, b varchar(10));
CREATE VIEW v_cidade AS SELECT id, nome FROM cidade;
SET FOREIGN_KEY_CHECKS=0;
INSERT INTO pais VALUES ('MZ','Moçambique','2020-01-01 10:00:00'), ('pt','Portugal','0000-00-00 00:00:00'), ('BR','Brasil 🇧🇷','2021-05-05');
INSERT INTO cidade (pais,nome,populacao,ativa,nivel,area,dens,ratio,fundada,abre,duracao,tipo,tags,info,foto,flag,mascara,ano,nota) VALUES
 ('MZ','Maputo',18446744073709551615,1,0,346.775,1.1,3.141592653589793,'1876-01-01','08:30:00','30:15:00','a','x,z','{"b":1,"a":[1,2]}',x'00FF10',b'1',b'10100101',1999,'linha1\nlinha2\ttab'),
 ('PT','Lisboa',500000,0,5,100.05,2.5,0.1,'0000-00-00','23:59:59','-01:00:00','',NULL,NULL,NULL,b'0',NULL,2000,CONCAT('com', CHAR(0), 'nul')),
 ('XX','Órfã',NULL,NULL,1,NULL,NULL,NULL,'2000-02-30',NULL,NULL,'b','y','{}',x'',NULL,NULL,NULL,'aspas '' e \\ barra');
INSERT INTO zona VALUES ('MZ',1,'norte'),('MZ',2,'sul'),('PT',1,'centro');
INSERT INTO ponto (pais,num,ref_desc) VALUES ('MZ',1,'norte'),('MZ',9,'sul'),(NULL,NULL,'nenhuma');
INSERT INTO sem_pk VALUES (1,'a'),(1,'a'),(2,NULL);
SET FOREIGN_KEY_CHECKS=1;

-- ===== Objetos de código =====
ALTER TABLE cidade ADD COLUMN grupo_id tinyint(1) DEFAULT 0;
CREATE TABLE registo (id int AUTO_INCREMENT PRIMARY KEY, cidade_id int unsigned, acao varchar(20), ativo tinyint(1) DEFAULT 1,
  quando datetime DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE contador (nome varchar(20) PRIMARY KEY, valor int NOT NULL DEFAULT 0);
INSERT INTO contador VALUES ('cidades', 0);

DELIMITER //
CREATE TRIGGER trg_cidade_valida BEFORE INSERT ON cidade FOR EACH ROW
BEGIN
  DECLARE n INT DEFAULT 0;
  IF NULLIF(TRIM(NEW.nome), '') IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Preencha o nome da cidade.';
  END IF;
  SET NEW.ativa = 1, NEW.nome = CONCAT(UPPER(LEFT(NEW.nome, 1)), SUBSTRING(NEW.nome, 2));
  SELECT COUNT(*) INTO n FROM cidade WHERE pais <=> NEW.pais;
  IF n >= 100 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Demasiadas cidades neste país.';
  END IF;
END//
CREATE TRIGGER trg_cidade_registo AFTER INSERT ON cidade FOR EACH ROW
BEGIN
  INSERT INTO registo (cidade_id, acao, ativo) VALUES (NEW.id, 'criada', 1);
  UPDATE contador SET valor = valor + 1 WHERE nome = 'cidades';
END//
CREATE TRIGGER trg_com_cursor AFTER DELETE ON cidade FOR EACH ROW
BEGIN
  DECLARE fim INT DEFAULT 0;
  DECLARE c CURSOR FOR SELECT id FROM registo WHERE cidade_id = OLD.id;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET fim = 1;
  OPEN c;
  CLOSE c;
END//
CREATE PROCEDURE encher_contador(IN quantos INT, OUT total INT)
BEGIN
  DECLARE i INT DEFAULT 0;
  ciclo: WHILE i < quantos DO
    SET i = i + 1;
    IF i > 1000 THEN LEAVE ciclo; END IF;
    UPDATE contador SET valor = valor + 1 WHERE nome = 'cidades';
  END WHILE ciclo;
  SELECT valor INTO total FROM contador WHERE nome = 'cidades';
END//
CREATE FUNCTION nome_completo(p_nome VARCHAR(80), p_pais VARCHAR(10)) RETURNS VARCHAR(100) DETERMINISTIC
BEGIN
  RETURN CONCAT(p_nome, ' (', IFNULL(p_pais, '?'), ')');
END//
CREATE EVENT limpar_registos ON SCHEDULE EVERY 1 DAY STARTS '2026-01-01 03:30:00'
DO DELETE FROM registo WHERE quando < NOW() - INTERVAL 90 DAY//
DELIMITER ;

CREATE VIEW v_cidade_pais AS
  SELECT c.id, c.nome, p.nome AS pais, IF(c.ativa = 1, 'sim', 'não') AS ativa, nome_completo(c.nome, c.pais) AS rotulo
  FROM cidade c LEFT JOIN pais p ON p.codigo = c.pais;
