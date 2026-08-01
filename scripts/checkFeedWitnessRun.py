from sqlalchemy import create_engine, text


engine = create_engine("postgresql+psycopg://syn:syn@192.168.1.126:5432/syn")
with engine.connect() as connection:
    rows = connection.execute(
        text(
            '''
            SELECT
                (SELECT count(*) FROM events) AS events,
                (SELECT count(*) FROM things) AS things,
                (SELECT count(*) FROM jobs) AS jobs,
                (SELECT count(*) FROM events WHERE name = 'feedWitness') AS feed_witnesses
            '''
        )
    ).mappings().one()
    print(dict(rows))
