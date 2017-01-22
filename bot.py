#!/usr/bin/env python
'''
    Bot for LinkedIn that starts by scanning a few pre-defined
    user profiles, then continues to search their featured
    connections by search term(s). The goal is to automate the
    "viewed" featured of LinkedIn so that recruiters and other
    professionals will see that you've viewed their profile.
'''
import logging
import json
import codecs
from random import randint, shuffle
from time import sleep
from pprint import pformat

from lxml import html
import urllib3
from requests import Session
from requests.adapters import HTTPAdapter

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logging.basicConfig(level=logging.DEBUG)
logging.getLogger('requests.packages.urllib3').setLevel(logging.WARNING)
logging.getLogger('selenium.webdriver.remote').setLevel(logging.WARNING)

# Authentication
LINKEDIN_USERNAME = ''
LINKEDIN_PASSWORD = ''
# Configuration
LINKEDIN_SEARCH_TERMS = ['technical recruiter', 'engineering recruiter']
LINKEDIN_START_PROFILES = []

# Connection
USER_AGENT = 'Mozilla/5.0 (Windows NT 6.3; WOW64; rv:34.0) ' \
             'Gecko/20100101 Firefox/34.0'
LINKEDIN_PROFILE_URL = 'https://www.linkedin.com/in/'

# Public profile
X_PROFILE_NAME = \
    '//div[@class="profile-overview-content"]' \
    '//h1[@id="name"]/text()'
# Public profile experiences
X_PROFILE_EXPERIENCES = '//section[@id="experience"]/ul/li'
X_EXPERIENCE_TITLE = './/header/h4[@class="item-title"]//text()'
X_EXPERIENCE_COMPANY = './/header/h5[@class="item-subtitle"]//text()'
# Public profile suggestions
X_PROFILE_SUGGESTIONS = \
    '//div[@id="aux"]//div[@class="browse-map"]' \
    '/ul/li[contains(@class, "profile-card")]'
X_SUGGESTION_NAME = './div[@class="info"]/h4[@class="item-title"]/a/text()'
X_SUGGESTION_URL = './div[@class="info"]/h4[@class="item-title"]/a/@href'
X_SUGGESTION_HEADLINE = './div[@class="info"]/p[@class="headline"]/text()'

# GatherProxy XPaths
P_GP_PROXIES = '//table[@id="tblproxy"]/tr'


def _single(arr):
    '''Returns the first array item or None'''
    return arr[0] if len(arr) > 0 else None


def find_by_xpath(driver, xpath):
    '''Finds an element by XPATH'''
    return WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, xpath)))


def authenticate(driver):
    '''Starts an authenticated session'''
    driver.get('https://www.linkedin.com/uas/login')
    find_by_xpath(driver, '//input[@name = "session_key"]').send_keys(
        LINKEDIN_USERNAME)
    find_by_xpath(driver, '//input[@name = "session_password"]').send_keys(
        LINKEDIN_PASSWORD)
    find_by_xpath(driver, '//input[@id = "btn-primary"]').click()
    sleep(2)


class ProxyCrawler(object):
    '''
        Crawles proxy list websites to harvest usable proxies
    '''
    def __init__(self):
        self.log = logging.getLogger('linkedout.proxy')
        self.proxies = self.crawl_gatherproxy()

    def crawl_gatherproxy(self):
        '''Scrape GatherProxy for proxies'''
        baseurl = 'http://gatherproxy.com/'
        searchurl = baseurl + 'proxylist/anonymity/?t=Elite'
        searchpage = 1
        session = self.get_session_connection(baseurl)
        proxies = list()
        # Search
        for searchpage in range(1, 3):
            self.log.debug('Fetching GatherProxy proxy list')
            self.log.debug('| page: %s', searchpage)
            res = session.request(
                url=searchurl,
                method='post',
                data='Type=elite&PageIdx=%s&Uptime=0' % (searchpage),
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded'
                })
            self.log.debug('|_status code: %s', res.status_code)
            if res.status_code is not 200:
                return None
            # Set up for XPath
            tree = html.fromstring(res.text)
            # Close our session
            session.close()
            # Find the person's name
            proxy_rows = tree.xpath(P_GP_PROXIES)[2:]
            for proxy_row in proxy_rows:
                raw_cols = proxy_row.xpath('./td/text()')
                proxy_cols = proxy_row.xpath('./td/script/text()')
                proxies.append({
                    'ip': proxy_cols[0].replace(
                        'document.write(\'', '').replace('\')', ''),
                    'port': int(proxy_cols[1].replace(
                        'document.write(gp.dep(\'', '').replace(
                            '\'))', ''), 16),
                    'location': raw_cols[2],
                    'speed': int(raw_cols[5].replace('ms', ''))
                })
        self.log.debug('Proxies: %s', pformat(proxies))

    @staticmethod
    def get_session_connection(baseurl):
        '''
            Creates a `requests.Session` instance
            and includes basic connection fault tolerance.

        :returns: A configured requests.Session instance
        :rtype: :class:`requests.Session`
        '''
        # Build a session object with some fault tolerance
        # Retry up to 10 times with increasing backoff time
        # up to 120 seconds.
        session = Session()
        session.mount(
            baseurl,
            HTTPAdapter(
                max_retries=urllib3.util.Retry(
                    total=10,
                    backoff_factor=0.4,
                    status_forcelist=[500, 501, 502, 503, 504]
                )))
        session.headers = {
            'User-Agent': USER_AGENT
        }
        return session


class LinkedInCrawler(object):
    '''
        Helper to crawl public LinkedIn pages
        and acting as a spider collecting information
        about profiles to feed to a viewer instance.

    :param list terms: List of search terms to crawl
    :param bool public: True for public profiles,
            False for authenticated / private profiles
    '''
    def __init__(self, terms=None, public=True):
        self.log = logging.getLogger('linkedout.crawler')
        self.terms = [x.lower() for x in terms or list()] or None
        self.public = public
        self._active_list = list()

    def build_profile_list(self, profile_urls, depth=1):
        '''
            Builds a list of public profile information
            based on recursive suggestion searches

        :param list profile_urls: List of public profiles to crawl from
        :param int depth: Suggestions crawl depth
        :returns: List of profiles
        :rtype: list
        '''
        self._active_list = list()
        people = list()
        # Normalize types
        if isinstance(profile_urls, basestring):
            profile_urls = [profile_urls]
        # Crawl profiles
        for profile_url in profile_urls:
            people.extend(self._build_profile_list(profile_url, depth))
        # Remove empty items and return
        return [x for x in people if x is not None]

    def _build_profile_list(self, profile_url, depth=1):
        '''Worker for build_profile_list()'''
        self.log.debug('build_profile_list(%s, %s)',
                       profile_url, depth)
        active = self.parse_public_profile(profile_url)
        if not active:
            return list()
        # Redundancy check list
        self._active_list.append(active.get('name'))
        self.log.debug('Active list: %s', self._active_list)
        people = [active]
        if depth:
            for suggestion in active.get('suggestions', list()):
                sname = suggestion.get('name')
                if not sname:
                    self.log.warn('A suggestion has no name')
                    continue
                # Check that there's a profile URL
                if not suggestion.get('url'):
                    self.log.warn('Suggestion "%s" missing URL', sname)
                    continue
                # Check that we're not repeating ourselves
                if suggestion.get('name') in set(self._active_list):
                    self.log.warn('Suggestion "%s" already crawled', sname)
                    continue
                # Weed out profiles that don't match the terms set
                if not suggestion.get('headline'):
                    self.log.warn('Suggestion "%s" has no headline', sname)
                    continue
                headline = suggestion.get('headline', '').lower()
                if self.terms and \
                   len([x for x in self.terms if x in headline]) < 1:
                    self.log.warn('Suggestion "%s" doesn\'t match terms set',
                                  sname)
                    continue
                # Act totally not like a Python bot
                sleep(randint(1, 10))
                # Enter recursion hell
                people.extend(self._build_profile_list(
                    suggestion['url'],
                    depth=depth-1))
        return people

    def parse_public_profile(self, profile_url):
        '''
            Parses a public user profile for key
            properties (name, experience, etc...)

        :returns: Profile information
        :rtype: dict
        '''
        session = self.get_session_connection()
        person = dict()
        # Normalize profile link
        if not profile_url.startswith(LINKEDIN_PROFILE_URL):
            profile_url = LINKEDIN_PROFILE_URL + profile_url
        self.log.debug('Fetching public profile')
        self.log.debug('| url: %s', profile_url)
        self.log.debug('| method: GET')
        # Request the page
        res = session.request(url=profile_url, method='get')
        self.log.debug('|_status code: %s', res.status_code)
        if res.status_code is not 200:
            return None
        # Set up for XPath
        tree = html.fromstring(res.text)
        # Close our session
        session.close()
        # Save the person's profile link
        person['url'] = profile_url
        # Find the person's name
        person['name'] = _single(tree.xpath(X_PROFILE_NAME))
        # Find experience
        person['experiences'] = list()
        for experience in tree.xpath(X_PROFILE_EXPERIENCES):
            person['experiences'].append({
                'title': _single(experience.xpath(X_EXPERIENCE_TITLE)),
                'company': _single(experience.xpath(X_EXPERIENCE_COMPANY)),
                'date': {
                    'start': _single(experience.xpath('.//time[1]/text()')),
                    'end': _single(experience.xpath('.//time[2]/text()'))
                }
            })
        # Find suggested profiles
        person['suggestions'] = list()
        for suggestion in tree.xpath(X_PROFILE_SUGGESTIONS):
            url = _single(suggestion.xpath(X_SUGGESTION_URL))
            if url:
                url = url.split('?')[0]
            person['suggestions'].append({
                'url': url,
                'name': _single(suggestion.xpath(X_SUGGESTION_NAME)),
                'headline': _single(suggestion.xpath(X_SUGGESTION_HEADLINE))
            })

        self.log.debug('Profile information: %s',
                       json.dumps(person, indent=2))
        return person

    @staticmethod
    def get_session_connection():
        '''
            Creates a `requests.Session` instance
            and includes basic connection fault tolerance.

        :returns: A configured requests.Session instance
        :rtype: :class:`requests.Session`
        '''
        # Build a session object with some fault tolerance
        # Retry up to 10 times with increasing backoff time
        # up to 120 seconds.
        session = Session()
        session.mount(
            LINKEDIN_PROFILE_URL,
            HTTPAdapter(
                max_retries=urllib3.util.Retry(
                    total=10,
                    backoff_factor=0.4,
                    status_forcelist=[500, 501, 502, 503, 504]
                )))
        session.headers = {
            'User-Agent': USER_AGENT
        }
        return session


def main():
    '''Entry point'''
    # Get a list of usable proxies
    # proxylist = ProxyCrawler()

    # Init the crawler
    crawler = LinkedInCrawler(LINKEDIN_SEARCH_TERMS)
    # Build a list to use for searching
    people = crawler.build_profile_list(LINKEDIN_START_PROFILES, depth=3)
    print 'People: %s' % json.dumps(people, indent=2)
    # Roll the dice, mix up the list
    shuffle(people)
    # Get a browser driver
    driver = webdriver.Chrome()
    # Get an authenticated session
    authenticate(driver)
    # Read in parsable list of profiles to start with
    for person in people:
        print 'Viewing profile: "%s"' % person['name']
        driver.get(person['url'])
        _sleep_cnt = randint(2, 45)
        print '|_sleeping %s seconds' % _sleep_cnt
        sleep(_sleep_cnt)
    print 'Successfully viewed %s profiles' % len(people)


if __name__ == "__main__":
    main()
