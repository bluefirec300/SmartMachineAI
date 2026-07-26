import sqlite3
from pathlib import Path


DB_PATH = Path("../database/machine_data.db")


def get_latest_value(tag):

    connection = sqlite3.connect(DB_PATH)

    cursor = connection.cursor()


    cursor.execute(
        """
        SELECT time, value
        FROM plc_data
        WHERE tag=?
        ORDER BY time DESC
        LIMIT 1
        """,
        (tag,)
    )


    result = cursor.fetchone()


    connection.close()


    return result



while True:

    question = input("\nAsk machine data: ")


    if question.lower() == "exit":
        break


    if "pressure" in question.lower():

        data = get_latest_value("Pressure")

        print(
            "Pressure =",
            data[1],
            "Updated:",
            data[0]
        )


    elif "temperature" in question.lower():

        data = get_latest_value("Temperature")

        print(
            "Temperature =",
            data[1],
            "Updated:",
            data[0]
        )


    elif "flow" in question.lower():

        data = get_latest_value("FlowRate")

        print(
            "FlowRate =",
            data[1],
            "Updated:",
            data[0]
        )


    else:

        print(
            "I don't understand the request"
        )
