from __future__ import division
NAME = "tweetdb"
VERSION = "0.1"
DESCRIPTION = """Utilities for storing tweets in an relational database."""
AUTHOR = "Russell Miller"
AUTHOR_EMAIL = ""
URL = "https://github.com/starkshift/tweetdb"
LICENSE = "MIT"

import tweepy
import pickle
import urllib3
import certifi
import zlib
import logging
import requests
import md5
import os
import re
from multiprocessing import Process
from yaml import load
from datetime import datetime as dt
from sqlalchemy import create_engine, ForeignKey
from sqlalchemy import Column, DateTime, Integer, String, Boolean, BigInteger, \
    Float, Binary
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound


# set up the sql base
Base = declarative_base()

# get rootLogger
log = logging.getLogger("__name__")


def read_parmdata(parmfile):
    # parse a YAML parameter file
    with open(parmfile, 'r') as f:
        return load(f)


def get_oauth(parmdata):
    # authenticate to twitter
    keys = pickle.load(open(parmdata['files']['twitter_keys'], 'rb'))
    auth = tweepy.OAuthHandler(keys['ConsumerKey'], keys['ConsumerSecret'])
    auth.set_access_token(keys['AccessToken'], keys['AccessTokenSecret'])
    return auth
    

def get_sql_engine(parmdata, echo=False):
    if parmdata['database']['db_type'].upper() == 'SQLITE':
        arg = 'sqlite:///' + parmdata['database']['db_host']
    elif parmdata['database']['db_type'].upper() == 'POSTGRES':
        dblogin = pickle.load(open(parmdata['database']['db_login'], 'rb'))
        arg = 'postgresql://' + dblogin['username'] + ':' + \
              dblogin['password'] + '@' \
              + parmdata['database']['db_host'] \
              + '/' + parmdata['database']['db_name']
    return create_engine(arg, echo=echo)


def get_sql_session(parmdata, echo=False):
    engine = get_sql_engine(parmdata, echo=echo)
    Session = sessionmaker(bind=engine)
    return Session()


def create_tables(engine):
    log.info('Creating database tables.')
    Base.metadata.create_all(engine)


def drop_tables(engine):
    dropflag = raw_input('WARNING: All tables in database will ' +
                         'be dropped.  Proceed? [y/N] ')
    if dropflag.upper() == 'Y':
        log.info('Dropping database tables.')
        Base.metadata.drop_all(engine)


def drop_images(parmdata):
    image_path = parmdata['settings']['image_storage']['path']
    dropflag = raw_input('WARNING: Image storage directory ' +
                         '(\'%s\') will be' % (image_path) +
                         'deleted.  Proceed? [y/N] ')
    if dropflag.upper() == 'Y':
        log.info('Remove image directory.')
        os.system('rm -fr "%s"' % image_path)


def read_timeline(engine, auth, parmdata, userid=None):
    api = tweepy.API(auth)
    
    try:
        # make session
        Session = sessionmaker(bind=engine)
        session = Session()

        # handle user info
        rawuser = api.get_user(userid)
        add_user(rawuser, session)

        # get tweets
        myCursor = tweepy.Cursor(api.user_timeline, id=userid)
        for rawtweet in myCursor.items():
            add_tweet(rawtweet, session, parmdata['settings']['get_images'])

        # commit
        session.commit()
    except:
        session.rollback()
        session.close()
        raise
  
    session.close()


def tweet_words(text):
    # make the text all lower case
    text = text.lower()

    # strip off URL data
    text = re.sub('((www\.[^\s]+)|(https?://[^\s]+))', '', text)

    # strip off mentions
    text = re.sub('@[^\s]+', '', text)

    # remove additional white spaces
    text = re.sub('[\s]+', ' ', text)

    # replace hashtags with a word
    text = re.sub(r'#([^\s]+)', r'\1', text)

    # trim
    text = text.strip('\'"')

    # split into words
    words = text.split()
    goodwords = []
    for word in words:
        word = word.strip('\'"?,.!;:')
        # check if the word stats with an alphabet
        val = re.search(r"^[a-zA-Z][a-zA-Z0-9]*$", word)

        # keep only words of length 2 or more
        if (val is None or len(word) < 3):
            continue
        else:
            goodwords.append(word)

    return goodwords


def add_tweet(tweet, session, get_images=False, image_path=None, https=None):
    # check if we've already added this tweet
    if session.query(Tweet).filter(Tweet.tweetid == tweet.id).count() == 0:
        tweetobj = Tweet(tweet)
        session.add(tweetobj)
      
        for tag in tweet.entities['hashtags']:
            hashobj = Hashtag(tweet, tag, session)
            session.merge(hashobj)
          
        for mention in tweet.entities['user_mentions']:
            mentionobj = Mention(tweet, mention)
            session.merge(mentionobj)
          
        for url in tweet.entities['urls']:
            urlobj = URLData(tweet, url)
            session.merge(urlobj)
          
        if tweet.geo is not None:
            geotagobj = Geotag(tweet)
            session.merge(geotagobj)
                
        if (get_images) and ('media' in tweet.entities):
            for idx, media in enumerate(tweet.entities['media']):
                mediaobj = Media(tweet, media, idx, image_path, https)
                session.merge(mediaobj)

        session.commit()

        # process words inside the tweet's body
        words = tweet_words(tweet.text)
        for word in words:
            wordobj = TweetWord(tweet.id, word, session)
            session.merge(wordobj)
        
        session.commit()
        

    else:
        tweetobj = session.query(Tweet).filter(Tweet.tweetid == tweet.id).one()
        tweetobj.update(tweet)
        session.add(tweetobj)
        session.commit()


def add_user(user, session):
    if session.query(User).filter(User.userid == user.id).count() == 0:
        userobj = User(user)
        session.add(userobj)
        session.commit()
    else:
        userobj = session.query(User).filter(User.userid == user.id).one()
        userobj.update(user)
        session.add(userobj)
        session.commit()
          

# class tweet_consumer(threading.Thread):
class tweet_consumer(Process):
    '''
    This class manages the queue of tweets waiting to be added to the
    SQL database
    '''

    # open https pool for grabbing url data
    https = urllib3.PoolManager(cert_reqs="CERT_REQUIRED",
                                ca_certs=certifi.where())

    def __init__(self, queue, engine, parmdata, name=None):
        # initialize the thread

        Process.__init__(self, name=name)
        
        log.info("Starting new tweet consumer.")
 
        # this is meant to be a daemon process, exiting when the program closes
        self.daemon = True

        # update the log from this thread at this interval
        self.log_interval = parmdata['settings']['log_interval']
 
        # bind this thread to the database
        log.info('Establishing database session..')
        self.session = get_sql_session(parmdata)

        # set the queue to pull tweets from
        self.queue = queue
        '''
        twitter's stream filtering for languages is currently (March 2015)
        broken (you need to have a search term in addition to a language
        in order to filter), but we want the full stream so we'll do the
        language filter ourselves
        '''
        self.languages = parmdata['settings']['langs']
        for lang in self.languages:
            log.info('Logging tweets of language \'%s\'.' % lang)

        # set up whether we're getting tweeted images or not
        self.get_images = parmdata['settings']['get_images']
        log.info('Logging image file data is set to \'%s\'.' % self.get_images)

        if parmdata['settings']['image_storage']['method'].upper() == 'FILE':
            self.image_path = parmdata['settings']['image_storage']['path']
            if self.get_images:
                log.info('Image data being stored on filesystem at \'%s\''
                         % self.image_path)
        
        # some diagnostic variables
        self.last_time = dt.now()
        self.n_tweets = 0
        self.n_dupes = 0

    def run(self):
        while True:
            status = self.queue.get()
            if any(status.lang in s for s in self.languages) or \
               any('ALL' in s.upper() for s in self.languages):
                '''
                There is a small chance that two threads will try
                to add the same user concurrently. This try statement
                serves to get arround the sqlalchemy IntegrityError
                which would result
                '''
                try:
                    add_user(status.author, self.session)
                    add_tweet(status, self.session, self.get_images,
                              self.image_path, self.https)
                    self.n_tweets += 1
                except IntegrityError:
                    self.n_dupes += 1
                    self.session.rollback()
                    pass
                except:
                    raise
            self.status_update()

    def status_update(self):
        '''
        Method for keeping track of the rate at which each tweet consumer is
        processing the queue
        '''
        elapsed_time = (dt.now() - self.last_time).total_seconds()
        if elapsed_time > self.log_interval:
            log.info("Consuming %f tweets/second (%f/sec discarded as duplicates)." %
                     (self.n_tweets/elapsed_time, self.n_dupes/elapsed_time))
            log.info("Reporting %d tweets remaining in queue." %
                     self.queue.qsize())
            self.last_time = dt.now()
            self.n_tweets = 0
            self.n_dupes = 0


class tweet_producer(Process):
    def __init__(self, auth, queue, parmdata, name=None):
        # initialize the thread
        Process.__init__(self, name=name)

        log.info("Starting new tweet producer.")
        self.auth = auth
        self.queue = queue
        self.parmdata = parmdata
        self.daemon = True
        self.active = True

    def run(self):
        while self.active:
            try:
                # set up twitter api
                self.api = tweepy.API(self.auth)
                
                # set up stream listener
                self.myListener = database_listener(self.api, self.queue,
                                                    self.parmdata['settings']['log_interval'])
                
                self.stream =  tweepy.streaming.Stream(self.auth,
                                                       self.myListener, timeout=60)
                log.info("Streaming API connected.  Adding tweets to queue.")
                self.stream.sample()
            except requests.packages.urllib3.exceptions.ProtocolError:
                pass
            except Exception as e:
                log.error(str(e))
                pass
 
    def close(self):
        log.info("Disconnecting Twitter stream.")
        self.stream.disconnect()
        self.active = False
        self.join()

###########################################################
#         Tweepy Listener Class Definitions
###########################################################


class database_listener(tweepy.StreamListener):
    '''
    Takes data received from the streaming API and places it in the
    queue to be processed by tweet_handlers
    '''

    def on_status(self, status):
        self.queue.put(status)
        self.n_count += 1
        self.status_update()
        return True
 
    def on_error(self, status_code):
        log.info('Got an error with status code: ' + str(status_code))
        return True  # To continue listening
 
    def on_timeout(self):
        log.info('Listener timeout.')
        return True   # To continue listening

    def status_update(self):
        '''
        Method for keeping track of the rate at which each tweet producer is
        feeding the queue
        '''
        elapsed_time = (dt.now() - self.last_time).total_seconds()
        if elapsed_time > self.log_interval:
            log.info("Producing %f tweets/second." %
                     (self.n_count/elapsed_time))
            self.last_time = dt.now()
            self.n_count = 0

    def __init__(self, api, queue, log_interval):
        self.api = api
        self.queue = queue
        self.n_count = 0
        self.log_interval = log_interval
        self.last_time = dt.now()
       
###########################################################
#            SQLAlchemy Class Definitions
###########################################################


class Hashtag(Base):
    """Hashtag Data"""
    __tablename__ = "Hashtag"
    hashid = Column('hashid', Integer, primary_key=True)
    tweetid = Column('tweetid', BigInteger, ForeignKey("Tweet.tweetid"),
                     unique=False, index=True)
    hashtagid = Column('hashtagid', Integer, ForeignKey("HashtagLexicon.hashtagid"),
                       unique=False, index=True)
        
    def __init__(self, tweet, tag, session):
        # check if the hashtag is already in the lexicon
        try:
            lexobj = session.query(HashtagLexicon).\
                    filter(HashtagLexicon.hashtagtext == tag['text']).one()
        except MultipleResultsFound, e:
            raise e
        except NoResultFound, e:
            lexobj = HashtagLexicon(tag)
            lexobj = session.merge(lexobj)
            session.flush()
        self.tweetid = tweet.id
        self.hashtagid = lexobj.hashtagid


class HashtagLexicon(Base):
    """Hashtag Text"""
    __tablename__ = "HashtagLexicon"
    hashtagid = Column('hashtagid', Integer, unique=True, primary_key=True,
                       index=True)
    hashtagtext = Column('hashtagtext', String, index=True, unique=True)

    def __init__(self, tag):
        self.hashtagtext = tag['text']


class TweetLexicon(Base):
    """Tweet Text"""
    __tablename__ = "TweetLexicon"
    wordid = Column('wordid', Integer, unique=True, primary_key=True,
                       index=True)
    wordtext = Column('wordtext', String, index=True, unique=True)

    def __init__(self, word):
        self.wordtext = word


class TweetWord(Base):
    """Tweet Text"""
    __tablename__ = "TweetWord"
    id = Column('id', Integer, unique=True, primary_key=True)
    tweetid = Column('tweetid', BigInteger, ForeignKey("Tweet.tweetid"),
                     unique=False, index=True)
    wordid = Column('wordid', Integer, index=True)

    def __init__(self, tweetid, word, session):
        jobdone = False
        while not jobdone:
            try:
                wordobj = session.query(TweetLexicon).\
                          filter(TweetLexicon.wordtext == word).one()
            except MultipleResultsFound, e:
                raise e
            except NoResultFound, e:
                try:
                    wordobj = TweetLexicon(word)
                    wordobj = session.merge(wordobj)
                    session.flush()
                except IntegrityError, e:
                    session.rollback()
                    pass
                except:
                    raise
            jobdone = True
        self.tweetid = tweetid
        self.wordid = wordobj.wordid


class Media(Base):
    """Binary Media Data"""
    __tablename__ = "Media"
    mediaid = Column('mediaid', Integer, primary_key=True)
    tweetid = Column('tweetid', BigInteger, ForeignKey("Tweet.tweetid"),
                     unique=False, index=True)
    blob = Column('blob', Binary, unique=False, nullable=True)
    native_filename = Column('native_filename', String, unique=False,
                             nullable=True)
    local_filename = Column('local_filename', String, unique=False,
                            nullable=True)
    
    def __init__(self, tweet, media, idx, image_path=None, https=None):
        self.tweetid = tweet.id
        rawdata = https.request('GET', media['media_url_https']).data
        extension = os.path.splitext(media['media_url_https'])[1]
        self.native_filename = os.path.split(media['media_url_https'])[1]
        if image_path is None:
            self.blob = zlib.compress(rawdata)
            self.filename = None
        else:
            self.blob = None
            md5hash = md5.new()
            md5hash.update(str(tweet.id) + str(idx))
            hashdata = md5hash.hexdigest()
            self.local_filename = image_path + os.path.sep + hashdata[0:2] +\
                                  os.path.sep + hashdata[3:5] + os.path.sep +\
                                  hashdata[6:8] + os.path.sep + hashdata[9:] \
                                  + extension
            if not os.path.exists(os.path.dirname(self.local_filename)):
                os.makedirs(os.path.dirname(self.local_filename), mode=0777)
            with open(self.local_filename, 'wb') as f:
                f.write(rawdata)
            

class URLData(Base):
    """URL Data"""
    __tablename__ = "URLData"
    urlid = Column('urlid', Integer, primary_key=True)
    tweetid = Column('tweetid', BigInteger, ForeignKey("Tweet.tweetid"),
                     unique=False, index=True)
    url = Column('url', String, unique=False)
    
    def __init__(self, tweet, url):
        self.tweetid = tweet.id
        self.url = url['expanded_url']


class Mention(Base):
    """User Mention Data"""
    __tablename__ = "Mention"
    mentionid = Column('mentionid', Integer, primary_key=True)
    tweetid = Column('tweetid', BigInteger, ForeignKey("Tweet.tweetid"),
                     unique=False, index=True)
    source = Column('source', BigInteger, unique=False, index=True)
    target = Column('target', BigInteger, unique=False, index=True)
    
    def __init__(self, tweet, mention):
        self.tweetid = tweet.id
        self.source = tweet.author.id
        self.target = mention['id']
 

class Geotag(Base):
    """Geotag Data"""
    __tablename__ = "Geotag"
    geoid = Column('geoid', Integer, primary_key=True)
    tweetid = Column('tweetid', BigInteger, ForeignKey("Tweet.tweetid"),
                     unique=False, index=True)
    latitude = Column('latitude', Float, unique=False)
    longitude = Column('longitude', Float, unique=False)
    
    def __init__(self, tweet):
        self.tweetid = tweet.id
        self.latitude = tweet.geo['coordinates'][0]
        self.longitude = tweet.geo['coordinates'][1]


class Tweet(Base):
    """Tweet Data"""
    __tablename__ = "Tweet"

    tweetid = Column(BigInteger, primary_key=True, index=True)
    userid = Column('userid', BigInteger, ForeignKey("User.userid"), index=True)
    text = Column('text', String(length=500), nullable=True)
    place = Column('place', String, index=True)
    rtcount = Column('rtcount', Integer)
    fvcount = Column('fvcount', Integer)
    lang = Column('lang', String, index=True)
    date = Column('date', DateTime, index=True)
    source = Column('source', String, index=True)
    geotags = relationship(Geotag, lazy="dynamic", backref='tweet')
    hashtags = relationship(Hashtag, lazy="dynamic", backref='tweet')
    mentions = relationship(Mention, lazy="dynamic", backref='tweet')
    urls = relationship(URLData, lazy="dynamic",  backref='tweet')
    media = relationship(Media, lazy="dynamic", backref='tweet')

    def __init__(self, tweet):
        self.tweetid = tweet.id
        self.userid = tweet.author.id
        self.text = tweet.text
        self.rtcount = tweet.retweet_count
        self.fvcount = tweet.favorite_count
        self.lang = tweet.lang
        self.date = tweet.created_at
        self.source = tweet.source

    def update(self, tweet):
        self.rtcount = tweet.retweet_count
        self.fvcount = tweet.favorite_count
  

class User(Base):
    """Twitter User Data"""
    __tablename__ = "User"

    userid = Column(BigInteger, primary_key=True, index=True)
    username = Column('username', String)
    name = Column('name', String)
    location = Column('location', String, nullable=True)
    description = Column('description', String, nullable=True)
    numfollowers = Column('numfollowers', Integer)
    numfriends = Column('numfriends', Integer)
    numtweets = Column('numtweets', Integer)
    createdat = Column('createdat', DateTime)
    timezone = Column('timezone', String)
    geoloc = Column('geoloc', Boolean)
    lastupdate = Column('lastupdate', DateTime)
    verified = Column('verified', Boolean)
    tweets = relationship(Tweet, lazy="dynamic", backref='user')

    def __init__(self, author):
        self.userid = author.id
        self.username = author.screen_name
        self.name = author.name
        self.location = author.location
        self.description = author.description
        self.numfollowers = author.followers_count
        self.numfriends = author.friends_count
        self.numtweets = author.statuses_count
        self.createdat = author.created_at
        self.timezone = author.time_zone
        self.geoloc = author.geo_enabled
        self.verified = author.verified
        self.lastupdate = dt.now()

    def update(self, author):
        self.username = author.screen_name
        self.name = author.name
        self.location = author.location
        self.description = author.description
        self.numfollowers = author.followers_count
        self.numfriends = author.friends_count
        self.numtweets = author.statuses_count
        self.timezone = author.time_zone
        self.geoloc = author.geo_enabled
        self.verified = author.verified
        self.lastupdate = dt.now()
