from pyspark import SparkConf, SparkContext
from pyspark.sql import SparkSession, Window
from pyspark.sql.functions import (
    col,
    lag,
    avg,
    exp,
    sum,
    log,
    stddev_pop,
    year,
    month,
    date_format,
    to_timestamp,
    sqrt,
    lit,
)
import os
import logging


def transform_stock_data(gcs_input_data_path: str, gcs_output_data_path: str):
    conf = (
        SparkConf()
        .setMaster("local[*]")
        .setAppName("test")
        .set("spark.jars", "/opt/spark/jars/gcs-connector-hadoop2-2.1.1.jar")
        .set("spark.hadoop.google.cloud.auth.service.account.enable", "true")
        .set(
            "spark.hadoop.google.cloud.auth.service.account.json.keyfile",
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        )
    )

    sc = SparkContext(conf=conf)

    sc._jsc.hadoopConfiguration().set(
        "fs.AbstractFileSystem.gs.impl", "com.google.cloud.hadoop.fs.gcs.GoogleHadoopFS"
    )
    sc._jsc.hadoopConfiguration().set(
        "fs.gs.impl", "com.google.cloud.hadoop.fs.gcs.GoogleHadoopFileSystem"
    )
    sc._jsc.hadoopConfiguration().set(
        "fs.gs.auth.service.account.json.keyfile",
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    )
    sc._jsc.hadoopConfiguration().set("fs.gs.auth.service.account.enable", "true")

    spark = (
        SparkSession.builder.master("local[*]")
        .appName("StockDataTransformation")
        .config(conf=sc.getConf())
        .getOrCreate()
    )
    spark.conf.set("mapreduce.fileoutputcommitter.marksuccessfuljobs", "false")

    # Read data from Google Cloud Storage
    df_spark = spark.read.csv(gcs_input_data_path, header=True, inferSchema=True)
    # Constants
    MIN_PERIODS = 75
    TRADING_DAYS = 252

    try:
        # Check if the input DataFrame is empty
        if df_spark.count() == 0:
            print("Dataframe is empty, nothing to transform")
            return None

        # Convert date column to timestamp format and set it as index
        df_spark = df_spark.withColumn(
            "date",
            to_timestamp(date_format(col("date"), "yyyy-MM-dd HH:mm:ss")).cast(
                "timestamp"
            ),
        )
        df_spark = df_spark.orderBy("date").repartition(10)
        df_spark.createOrReplaceTempView("stock_data")

        df_spark = spark.sql("SELECT * FROM stock_data SORT BY date")

        # Calculate daily percentage change, rolling averages, standard deviation and Bollinger bands
        window1 = Window.partitionBy("symbol").orderBy("date")
        window2 = Window.partitionBy("symbol").orderBy("date").rowsBetween(-19, 0)
        window3 = Window.partitionBy("symbol").orderBy("date").rowsBetween(-199, 0)
        window4 = (
            Window.partitionBy("symbol")
            .orderBy("date")
            .rowsBetween(-(MIN_PERIODS - 1), 0)
        )
        window5 = (
            Window.partitionBy("symbol")
            .orderBy("date")
            .rowsBetween(-(TRADING_DAYS - 1), 0)
        )
        df_spark = df_spark.withColumn(
            "daily_pct_change", col("adjClose") / lag("adjClose", 1).over(window1) - 1
        )
        df_spark = df_spark.withColumn(
            "twenty_day_moving", avg(col("adjClose")).over(window2)
        )
        df_spark = df_spark.withColumn(
            "two_hundred_day_moving", avg(col("adjClose")).over(window3)
        )
        df_spark = df_spark.withColumn("std", stddev_pop(col("adjClose")).over(window2))
        df_spark = df_spark.withColumn(
            "bollinger_up", col("twenty_day_moving") + col("std") * 2
        )
        df_spark = df_spark.withColumn(
            "bollinger_down", col("twenty_day_moving") - col("std") * 2
        )

        # Calculate cumulative daily and monthly returns, daily log returns, volatility, and Sharpe ratio
        df_spark = df_spark.withColumn(
            "cum_daily_returns",
            exp(sum(log(1 + col("daily_pct_change"))).over(window1)),
        )
        df_spark = df_spark.withColumn(
            "cum_monthly_returns",
            avg(col("cum_daily_returns")).over(
                Window.partitionBy(year(col("date")), month(col("date")))
            ),
        )
        df_spark = df_spark.withColumn(
            "daily_log_returns", log(col("daily_pct_change") + 1)
        )
        df_spark = df_spark.withColumn(
            "volatility",
            stddev_pop(col("adjClose")).over(window4) * sqrt(lit(MIN_PERIODS)),
        )
        df_spark = df_spark.withColumn(
            "returns", log(col("adjClose") / lag("adjClose", 1).over(window1))
        )
        df_spark = df_spark.withColumn(
            "sharpe_ratio",
            stddev_pop(col("returns")).over(window5)
            * sqrt(lit(TRADING_DAYS))
            / avg(col("returns")).over(window5),
        )

        # Write the transformed DataFrame to a CSV file in GCS
        df_spark.coalesce(1).write.mode("overwrite").option("header", "true").csv(
            gcs_output_data_path
        )

        print(f"writing transformed data to gcs at  : {gcs_output_data_path }")
    except Exception as e:
        # Log the error message
        logging.error("An error occurred during transformation: " + str(e))
        return None

    finally:
        # Stop the Spark session
        spark.stop()
