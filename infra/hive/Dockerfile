# Hive Metastore autonome, avec de quoi parler à PostgreSQL et à un stockage S3.
ARG HIVE_VERSION=4.0.1
FROM apache/hive:${HIVE_VERSION}

# Versions alignées sur le Hadoop 3.3.6 embarqué dans Hive 4.0.1.
ARG POSTGRES_JDBC_VERSION=42.7.4
ARG HADOOP_AWS_VERSION=3.3.6
ARG AWS_SDK_BUNDLE_VERSION=1.12.367
ARG MAVEN=https://repo1.maven.org/maven2

# --chmod : un ADD depuis une URL crée un fichier 600 root, illisible par l'utilisateur `hive`.
ADD --chmod=644 ${MAVEN}/org/postgresql/postgresql/${POSTGRES_JDBC_VERSION}/postgresql-${POSTGRES_JDBC_VERSION}.jar /opt/hive/lib/
ADD --chmod=644 ${MAVEN}/org/apache/hadoop/hadoop-aws/${HADOOP_AWS_VERSION}/hadoop-aws-${HADOOP_AWS_VERSION}.jar /opt/hive/lib/
ADD --chmod=644 ${MAVEN}/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK_BUNDLE_VERSION}/aws-java-sdk-bundle-${AWS_SDK_BUNDLE_VERSION}.jar /opt/hive/lib/
