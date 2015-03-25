from __future__ import division
NAME = "TweetDB"
VERSION = "0.1"
DESCRIPTION = """Utilities for storing tweets in an sqlite db"""
AUTHOR = "Russell Miller"
AUTHOR_EMAIL = ""
URL = ""
LICENSE = "Gnu GPL v3"

import tweepy
import sqlite3
import pickle
import urllib3
import certifi
import zlib
import logging
import requests
from datetime import datetime as dt
from sqlalchemy import create_engine, ForeignKey
from sqlalchemy import Column, Date, DateTime, Integer, String, Boolean, BigInteger, Float, Binary
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref, sessionmaker

Base = declarative_base()

# load the auth pickle
keys = pickle.load(open("mykeys.p","rb"))

# authenticate to the twitter api
auth = tweepy.OAuthHandler(keys['ConsumerKey'],keys['ConsumerSecret'])
auth.set_access_token(keys['AccessToken'],keys['AccessTokenSecret'])
api = tweepy.API(auth)

# connect to sqlite db
dbfile='sqlite:///twitter.db'
engine = create_engine(dbfile,echo=False)

# open https pool for grabbing url data
https = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=certifi.where())

def main():
    FORMAT = "%(asctime)-15s %(message)s"
    logging.basicConfig(filename='TweetDB.log', level=logging.INFO, format=FORMAT)
    stream_to_db()

def create_tables():
    Base.metadata.create_all(engine)

def drop_tables():
    Base.metadata.drop_all(engine)

def read_timeline(userid=None):
    try:
       # make session
       Session = sessionmaker(bind=engine)
       session = Session()

       # handle user info
       rawuser = api.get_user(userid)
       add_user(rawuser,session)

       # get tweets
       myCursor = tweepy.Cursor(api.user_timeline,id=userid)
       for rawtweet in myCursor.items():
           add_tweet(rawtweet,session)

       # commit
       session.commit()
    except:
       session.rollback()
       session.close()
       raise
  
    session.close()

def add_tweet(tweet,session):
   # check if we've already added this tweet
   if session.query(Tweet).filter(Tweet.tweetid == tweet.id).count() == 0:
      tweetobj = Tweet(tweet)
      session.add(tweetobj)
      session.commit()

      for tag in tweet.entities['hashtags']:
          hashobj = Hashtag(tweet,tag)
          session.merge(hashobj)
          session.commit()

      for mention in tweet.entities['user_mentions']:
          mentionobj = Mention(tweet,mention)
          session.merge(mentionobj)
          session.commit()

      for url in tweet.entities['urls']:
          urlobj = URLData(tweet,url)
          session.merge(urlobj)
          session.commit()

      if tweet.geo is not None:
          geotagobj = Geotag(tweet)
          session.merge(geotagobj)
          session.commit()
      '''
      if 'media' in tweet.entities:
          for media in tweet.entities['media']:
              mediaobj = Media(tweet,media)
              session.merge(mediaobj)
              session.commit()
      '''
   else:
      tweetobj = session.query(Tweet).filter(Tweet.tweetid==tweet.id).one()
      tweetobj.update(tweet)
      session.add(tweetobj)
      session.commit()


def add_user(user,session):
    if session.query(User).filter(User.userid==user.id).count() == 0:
          userobj = User(user)
          session.add(userobj)
          session.commit()
    else:
          userobj = session.query(User).filter(User.userid==user.id).one()
          userobj.update(user)
          session.add(userobj)
          session.commit()
          

def stream_to_db():
    languages = ['en','unk']
    while True: 
       try:
          # make session
          Session = sessionmaker(bind=engine)
          session = Session()
 
          myListener = database_listener(session,languages)
          stream = tweepy.streaming.Stream(auth,myListener,timeout=60)
          stream.sample()
       except KeyboardInterrupt:
          stream.disconnect()
          myListener.session.close()
          break
       except requests.packages.urllib3.exceptions.ProtocolError:
          continue
       except:
          continue
    session.close()     


###########################################################
#         Tweepy Listener Class Definitions
###########################################################
class database_listener(tweepy.StreamListener):
    ''' Handles data received from the stream. '''
 
    update_time = 10  # seconds
    n_total = 0
    n_valid = 0
    last_time = dt.now()
   
    def on_status(self, status):
        self.n_total+=1
        if any(status.lang in s for s in self.languages):
            self.n_valid+=1
            add_user(status.author,self.session)
            add_tweet(status,self.session)
        self.status_update()
        return True
 
    def on_error(self, status_code):
        print('Got an error with status code: ' + str(status_code))
        return True # To continue listening
 
    def on_timeout(self):
        print('Timeout...')
        return True # To continue listening

    def __init__(self,session,languages):
        self.session = session
        self.api = api
        self.languages = languages

    def status_update(self):
        elapsed_time = (dt.now() - self.last_time).total_seconds()
        if elapsed_time > self.update_time:
            logging.info("Capturing %f tweets/second (%f tweets/second after filtering, %f elapsed time)"%(self.n_total/elapsed_time,self.n_valid/elapsed_time,elapsed_time))
            self.last_time = dt.now()
            self.n_total = 0
            self.n_valid = 0


###########################################################
#            SQLAlchemy Class Definitions
###########################################################       
class Hashtag(Base): 
    """Hashtag Data"""
    __tablename__ = "Hashtag"
    tweetid = Column('tweetid',BigInteger,ForeignKey("Tweet.tweetid"),unique=False,primary_key=True)
    tag = Column('tag',String,unique=False)
    
    def __init__(self,tweet,tag):
        self.tweetid = tweet.id
        self.tag = tag['text']

class Media(Base): 
    """Binary Media Data"""
    __tablename__ = "Media"
    tweetid = Column('tweetid',BigInteger,ForeignKey("Tweet.tweetid"),unique=False,primary_key=True)
    mediatype = Column('mediatype',String,unique=False)
    blob = Column('blob',Binary,unique=False)
    
    def __init__(self,tweet,media):
        self.tweetid = tweet.id
        self.mediatype= media['type']
        rawdata = https.request('GET',media['media_url_https']).data
        self.blob = zlib.compress(rawdata)

class URLData(Base): 
    """URL Data"""
    __tablename__ = "URLData"
    tweetid = Column('tweetid',BigInteger,ForeignKey("Tweet.tweetid"),unique=False,primary_key=True)
    url = Column('url',String,unique=False)
    
    def __init__(self,tweet,url):
        self.tweetid = tweet.id
        self.url = url['expanded_url']

class Mention(Base):
    """User Mention Data"""
    __tablename__ = "Mention"
    tweetid = Column('tweetid',BigInteger,ForeignKey("Tweet.tweetid"),unique=False,primary_key=True)
    source = Column('source',Integer,unique=False)
    target = Column('target',Integer,unique=False)
    
    def __init__(self,tweet,mention):
        self.tweetid = tweet.id
        self.source = tweet.author.id
        self.target = mention['id']
 

class Geotag(Base):
    """Geotag Data"""
    __tablename__ = "Geotag"
    tweetid = Column('tweetid',BigInteger,ForeignKey("Tweet.tweetid"),unique=False,primary_key=True)
    latitude = Column('latitude',Float,unique=False)
    longitude = Column('longitude',Float,unique=False)
    
    def __init__(self,tweet):
        self.tweetid = tweet.id
        self.latitude = tweet.geo['coordinates'][0] 
        self.longitude = tweet.geo['coordinates'][1]  

class Tweet(Base):
    """Tweet Data"""
    __tablename__ = "Tweet"

    tweetid = Column(BigInteger,primary_key=True)
    userid = Column('userid',Integer,ForeignKey("User.userid"))
    text = Column('text',String(length=140),nullable=True)
    rtcount = Column('rtcount',Integer)
    fvcount = Column('fvcount',Integer)
    lang = Column('lang',String)
    date = Column('date',DateTime)
    source = Column('source',String)
    geotags = relationship(Geotag,primaryjoin=tweetid==Geotag.tweetid,lazy="dynamic")
    hashtags = relationship(Hashtag,primaryjoin=tweetid==Hashtag.tweetid,lazy="dynamic")
    mentions = relationship(Mention,primaryjoin=tweetid==Mention.tweetid,lazy="dynamic")
    urls = relationship(URLData,primaryjoin=tweetid==URLData.tweetid,lazy="dynamic")
    media = relationship(Media,primaryjoin=tweetid==Media.tweetid,lazy="dynamic")

    def __init__(self,tweet):
        self.tweetid = tweet.id
        self.userid = tweet.author.id
        self.text = tweet.text
        self.rtcount = tweet.retweet_count
        self.fvcount = tweet.favorite_count
        self.lang = tweet.lang
        self.date = tweet.created_at
        self.source = tweet.source

    def update(self,tweet):
        self.rtcount = tweet.retweet_count
        self.fvcount = tweet.favorite_count
  

class User(Base):
    """Twitter User Data"""
    __tablename__ = "User"

    userid = Column(Integer,primary_key=True)
    username = Column('username',String)
    location = Column('location',String,nullable=True)
    description = Column('description',String,nullable=True)
    numfollowers = Column('numfollowers',Integer)
    numfriends = Column('numfriends',Integer)
    numtweets = Column('numtweets',Integer)
    geoloc = Column('geoloc',Boolean)
    lastupdate = Column('lastupdate',DateTime)
    tweets = relationship(Tweet,primaryjoin=userid==Tweet.userid,lazy="dynamic")

    def __init__(self,author):  
        self.userid = author.id
        self.username = author.name
        self.location = author.location
        self.description = author.description
        self.numfollowers = author.followers_count
        self.numfriends = author.friends_count
        self.numtweets = author.statuses_count
        self.geoloc = author.geo_enabled
        self.lastupdate = dt.now()

    def update(self,author):  
        self.username = author.name
        self.location = author.location
        self.description = author.description
        self.numfollowers = author.followers_count
        self.numfriends = author.friends_count
        self.numtweets = author.statuses_count
        self.geoloc = author.geo_enabled
        self.lastupdate = dt.now()
